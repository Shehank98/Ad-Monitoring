"""
agent_cycle (Phase 3 a, c; S5, S7, S10). Autonomy level 0: observe and rehearse only.

run_cycle(now=None), every 15 minutes (railway/cron-agent.json):
  0. cycle-wide lock (PostgreSQL session advisory lock, ScopeLockRow elsewhere); a second
     cycle exits at once and logs AgentRun(kind='cycle', outcome='overlap_skipped')
  1. require_service_user() (never sync_service_user: that writes accounts.*, S8)
  2. kill switch: AgentConfig.enabled off -> heartbeat only
  3. sync_scopes() (agent table; strings copied from Schedule)
  4. selection, capped at max_scopes_per_cycle: needs_run, then inputs changed (scope
     fingerprint differs from the last observed snapshot), then due by time
     (observe_every_minutes). Account parts of the fingerprint are computed once per account
     (S10); if fingerprinting passes 60 s it stops and selection falls back to needs_run +
     due by time. Debounced scopes are skipped (outcome 'debounced').
  5. per scope:
       a. READ ONLY guard (rolled back): readiness, build_summary_data(schedule_id) per active
          schedule, fingerprint, diagnose, V1, V5 against the previous OBSERVED snapshot
       b. agent tables: observed snapshots, findings ledger + proposals, ScopeState,
          ScheduleStatus; needs_run cleared only here, after the observed snapshot
       c. shadow run only inside the nightly window, when dry runs are allowed (PostgreSQL),
          within the night budget, and never for authorised or locked scopes (S5)
     busy_yielded / timeout_yielded are recorded and never retried in the same cycle;
     any other DB error -> close_old_connections() and the next scope. A write inside a
     READ ONLY block (CoreWriteAttempt) stops the cycle: stop and ask.
  6. shadow window bookkeeping (core fingerprint at start and end), digest, heartbeat.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models import Max
from django.utils import timezone

from verification.tc_engine import _build_tc_theme_map, build_summary_data

from . import checks, core_fingerprint, gate, ledger, validate
from .canonical import sha256_of, to_jsonable
from .db import LOCK_NOT_AVAILABLE, QUERY_CANCELED, READ_ONLY_TXN, CoreWriteAttempt, Yielded, guard, pgcode
from .diagnose import Finding, diagnose
from .fingerprint import account_parts, fingerprint, fingerprint_sha, scope_context, scope_parts
from .heartbeat import beat_error
from .locks import ScopeBusy, _acquire_row, _release_row, is_postgres
from .models import (
    AgentAction, AgentAuthorisation, AgentConfig, AgentRun, Heartbeat, ScheduleStatus, ScopeState,
    SummarySnapshot,
)
from .readiness import assess
from .scope import active_schedules, sync_scopes
from .service import ServiceUserError, require_service_user
from .snapshots import shadow as shadow_run, store_observed

log = logging.getLogger('agent.cycle')

HEARTBEAT = 'agent_cycle'
CYCLE_LOCK_KEY = int.from_bytes(hashlib.sha256(b'agent_cycle').digest()[:8], 'big', signed=True)
CYCLE_LOCK_TTL_MINUTES = 30
SLOW_CYCLE_SECONDS = 600            # S7: overview alert above 10 minutes
FINGERPRINT_BUDGET_SECONDS = 60     # S10
YIELDS = ('busy_yielded', 'timeout_yielded')


class CycleBusy(Exception):
    pass


# ── time ──────────────────────────────────────────────────────────────────────

def night_of(now, cfg) -> tuple:
    """(night_date, window_start, window_end, inside) for the latest window start <= now,
    in Asia/Colombo (settings.TIME_ZONE). A window that crosses midnight is handled."""
    local = timezone.localtime(now)
    s, e = cfg.shadow_window_start, cfg.shadow_window_end
    night = local.date() if local.time() >= s else local.date() - timedelta(days=1)
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(night, s), tz)
    end_date = night if e > s else night + timedelta(days=1)
    end = timezone.make_aware(datetime.combine(end_date, e), tz)
    return night, start, end, start <= local < end


def dry_runs_allowed() -> bool:
    return connection.vendor == 'postgresql' or getattr(settings, 'AGENT_ALLOW_SQLITE_DRY_RUN', False)


# ── cycle lock (S7) ───────────────────────────────────────────────────────────

class _CycleLock:
    def __enter__(self):
        if is_postgres():
            with connection.cursor() as cur:
                cur.execute('SELECT pg_try_advisory_lock(%s)', [CYCLE_LOCK_KEY])
                (ok,) = cur.fetchone()
            if not ok:
                raise CycleBusy('another agent_cycle holds the cycle lock')
            self.owner = None
        else:
            try:
                self.owner = _acquire_row(CYCLE_LOCK_KEY, CYCLE_LOCK_TTL_MINUTES)
            except ScopeBusy as exc:
                raise CycleBusy(str(exc)) from exc
        return self

    def __exit__(self, *exc):
        if is_postgres():
            with connection.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock(%s)', [CYCLE_LOCK_KEY])
        else:
            _release_row(CYCLE_LOCK_KEY, self.owner)
        return False


# ── selection ─────────────────────────────────────────────────────────────────

@dataclass
class Pick:
    scope: ScopeState
    why: str                  # needs_run | changed | shadow_due | due


@dataclass
class Selection:
    picks: list = field(default_factory=list)
    fingerprint_seconds: float = 0.0
    fingerprint_fallback: bool = False
    candidates: int = 0


def _last_ok_scope_runs() -> dict:
    return dict(AgentRun.objects.filter(kind='scope', status='ok', scope__isnull=False)
                .values('scope_id').annotate(m=Max('started_at')).values_list('scope_id', 'm'))


def _last_observed_fp() -> dict:
    """{scope_id: fingerprint_sha256 of its latest observed snapshot}."""
    out = {}
    for sid, fp in (SummarySnapshot.objects.filter(kind='observed').order_by('scope_id', '-created_at', '-id')
                    .values_list('scope_id', 'fingerprint_sha256')):
        out.setdefault(sid, fp)
    return out


def _shadowed_since(since) -> set:
    """Scopes whose shadow step finished (ok, or deliberately skipped) since `since`.
    A yielded or failed shadow step is tried again by a later cycle of the same night."""
    done = set()
    for sid, d in AgentRun.objects.filter(kind='scope', status='ok', started_at__gte=since) \
            .values_list('scope_id', 'detail'):
        if ((d or {}).get('shadow') or {}).get('outcome') in ('ok', 'skipped'):
            done.add(sid)
    return done


def select(scopes, now, cfg, shadow_since=None) -> Selection:
    """Priority: needs_run, changed, shadow_due (in the window: not yet rehearsed tonight), due."""
    sel = Selection(candidates=len(scopes))
    last_run, last_fp = _last_ok_scope_runs(), _last_observed_fp()
    # run timestamps are wall-clock, so 'due' is measured on the wall clock too (not `now`,
    # which a synthetic walk-through may set)
    due_before = timezone.now() - timedelta(minutes=cfg.observe_every_minutes)
    shadowed = _shadowed_since(shadow_since) if shadow_since is not None else None
    needs, changed, due, shadow_due = [], [], [], []
    t0 = time.monotonic()
    acct_cache = {}
    for sc in scopes:
        if sc.needs_run:
            needs.append(sc)
            continue
        lr = last_run.get(sc.id)
        if lr is None or lr <= due_before:
            due.append((lr or datetime.min.replace(tzinfo=dt_timezone.utc), sc))
            continue
        if shadowed is not None and sc.id not in shadowed:
            shadow_due.append(sc)
            continue
        if sel.fingerprint_fallback or sc.id not in last_fp:
            continue
        try:
            if sc.account_id not in acct_cache:
                acct_cache[sc.account_id] = account_parts(sc.account_id)
            fp = {**acct_cache[sc.account_id], **scope_parts(sc)}
        except DatabaseError:
            _recover()
            continue
        if fingerprint_sha(fp) != last_fp[sc.id]:
            changed.append(sc)
        if time.monotonic() - t0 > FINGERPRINT_BUDGET_SECONDS:
            sel.fingerprint_fallback = True        # S10: record and fall back
    sel.fingerprint_seconds = round(time.monotonic() - t0, 3)
    ordered = ([Pick(s, 'needs_run') for s in sorted(needs, key=lambda s: (s.updated_at, s.id))]
               + [Pick(s, 'changed') for s in changed]
               + [Pick(s, 'shadow_due') for s in shadow_due]
               + [Pick(s, 'due') for _, s in sorted(due, key=lambda x: (x[0], x[1].id))])
    sel.picks = ordered
    return sel


# ── per scope ─────────────────────────────────────────────────────────────────

def _recover():
    """S7: after a database error, drop a broken connection (never inside an outer atomic
    block, e.g. a test transaction) and carry on with the next scope."""
    if not connection.in_atomic_block:
        close_old_connections()


def _yield_reason(exc) -> str | None:
    if isinstance(exc, Yielded):
        return exc.reason
    if isinstance(exc, ScopeBusy):
        return 'busy_yielded'
    code = pgcode(exc)
    if code == LOCK_NOT_AVAILABLE:
        return 'busy_yielded'
    if code == QUERY_CANCELED:
        return 'timeout_yielded'
    if code == READ_ONLY_TXN:
        raise CoreWriteAttempt(f'write attempted inside a read-only agent transaction: {exc}') from exc
    return None


def _prev_observed(scope):
    return (SummarySnapshot.objects.filter(scope=scope, kind='observed')
            .order_by('-created_at', '-id').first())


def observe_reads(scope, today) -> dict:
    """Everything the cycle reads for one scope, in ONE read-only rolled-back guard."""
    with guard(read_only=True):
        r = assess(scope, today=today)
        active = active_schedules(scope)
        data = {s.id: to_jsonable(build_summary_data(scope.account_id, scope.channel, scope.month,
                                                     schedule_id=s.id)) for s in active}
        fp = fingerprint(scope)
        ctx = scope_context(scope)
        findings = diagnose(scope, r)
        if r.reason == 'tc_not_linked':
            findings.append(Finding('TC_NOT_LINKED', 'A TC for this scope is not linked to its schedule.',
                                    tier=4, severity='bad'))
        tc_map = _build_tc_theme_map(scope.account_id)
        v1 = {s.id: [c for c in validate.v1_rows(data[s.id], scope.account_id, tc_map) if not c.ok]
              for s in active}
        v5 = [validate.v5(scope, s, sha256_of(data[s.id]), fp, ctx) for s in active]
        authorised = (r.state == 'AUTHORISED'
                      or AgentAuthorisation.objects.filter(schedule__in=active).exists())
        locked = bool(checks.locked_schedules(scope.account_id, scope.channel, scope.month))
    return {'readiness': r, 'active': active, 'data': data, 'fp': fp, 'ctx': ctx, 'findings': findings,
            'v1_failed': {str(k): [c.detail for c in v] for k, v in v1.items() if v},
            'v5': v5, 'authorised': authorised, 'locked': locked}


def _save_state(scope, reads, v5_unexplained, baselines):
    r = reads['readiness']
    scope.state = 'NEEDS_HUMAN' if v5_unexplained else r.state
    scope.reason = 'unexplained_change' if v5_unexplained else (r.reason or '')
    scope.needs_run = False                       # only after the observed snapshot (plan a.7)
    scope.save(update_fields=['state', 'reason', 'needs_run', 'updated_at'])
    linked = checks.makeup_linked_ids([s.schedule.id for s in r.schedules])
    for s in r.schedules:
        ScheduleStatus.objects.update_or_create(
            schedule=s.schedule,
            defaults={'scope': scope, 'sub_status': s.sub_status, 'has_tc': s.has_tc,
                      'makeup_linked': s.schedule.id in linked,
                      'baseline_reason': baselines.get(str(s.schedule.id), ''),
                      'matched_count': s.matched, 'pending_count': s.pending})


def observe_scope(scope, now, actor, run) -> dict:
    """Steps a + b. Returns detail for the scope AgentRun; raises on DB errors."""
    t0 = time.monotonic()
    prev = _prev_observed(scope)
    reads = observe_reads(scope, timezone.localtime(now).date())
    t_read = time.monotonic() - t0
    v5 = reads['v5']
    unexplained = [c.detail['schedule_id'] for c in v5 if not c.ok]
    baselines = {str(c.detail['schedule_id']): c.detail['baseline'] for c in v5 if c.detail.get('baseline')}
    with transaction.atomic():                    # agent tables only, short
        snaps = store_observed(scope, reads['data'], reads['fp'], reads['active'], run=run)
        stats = ledger.observe(scope, reads['findings'], prev.fingerprint if prev else None, reads['fp'],
                               reads['ctx'], actor=actor, now=now)
        _save_state(scope, reads, bool(unexplained), baselines)
    return {'reads': reads, 'snapshots': snaps, 'detail': {
        'state': scope.state, 'reason': scope.reason, 'schedules': [s.id for s in reads['active']],
        'observed_snapshot_ids': {str(k): v.id for k, v in snaps.items()},
        'v1_failed': reads['v1_failed'], 'v5_unexplained': unexplained, 'baselines': baselines,
        'v5': [{'ok': c.ok, **{k: c.detail.get(k) for k in ('schedule_id', 'changed', 'explained_by', 'baseline')}}
               for c in v5],
        'findings': stats, 'read_seconds': round(t_read, 3),
        'authorised': reads['authorised'], 'locked': reads['locked']}}


def shadow_scope(scope, obs, budget_left: float) -> dict:
    """Step c. Returns {'outcome', 'seconds', ...}. Never raises for a yield."""
    reads = obs['reads']
    if reads['authorised']:
        return {'outcome': 'skipped', 'reason': 'authorised', 'seconds': 0}       # S5
    if reads['locked']:
        return {'outcome': 'skipped', 'reason': 'schedule_locked', 'seconds': 0}  # S5
    if reads['readiness'].state == 'NEEDS_HUMAN' or not reads['active']:
        return {'outcome': 'skipped', 'reason': reads['readiness'].reason or 'no_active_schedule', 'seconds': 0}
    if budget_left <= 0:
        return {'outcome': 'skipped', 'reason': 'budget_exhausted', 'seconds': 0}
    t0 = time.monotonic()
    try:
        res = shadow_run(scope, obs['snapshots'])
    except CoreWriteAttempt:
        raise
    except Exception as exc:                        # noqa: BLE001
        reason = _yield_reason(exc)
        secs = round(time.monotonic() - t0, 3)
        if reason:
            _recover()
            return {'outcome': reason, 'seconds': secs}
        _recover()
        return {'outcome': 'failed', 'seconds': secs, 'error': f'{type(exc).__name__}: {exc}'[:500]}
    return {'outcome': 'ok', 'seconds': round(time.monotonic() - t0, 3), 'status': res.get('status'),
            'reason': res.get('reason', '')}


# ── shadow window bookkeeping (Phase 3 d) ─────────────────────────────────────

def _window_run(night):
    return AgentRun.objects.filter(kind='shadow_window', detail__night=night.isoformat()).order_by('-id').first()


def open_window(night, start, end) -> AgentRun:
    run = _window_run(night)
    if run is None:
        run = AgentRun.objects.create(kind='shadow_window', status='running', detail={
            'night': night.isoformat(), 'window_start': start.isoformat(), 'window_end': end.isoformat(),
            'used_seconds': 0.0, 'dry_runs': 0, 'label': core_fingerprint.LABEL,
            'start': core_fingerprint.take()})
    return run


def close_windows(now) -> list:
    """Close every shadow-window run whose window has ended: end fingerprint + diff + the
    AgentActions created inside the window (level 0: there must be none)."""
    closed = []
    for run in AgentRun.objects.filter(kind='shadow_window', status='running'):
        d = dict(run.detail)
        end = datetime.fromisoformat(d['window_end'])
        if now < end:
            continue
        start = datetime.fromisoformat(d['window_start'])
        d['end'] = core_fingerprint.take()
        d['diff'] = core_fingerprint.diff(d.get('start'), d['end'])
        acts = AgentAction.objects.filter(created_at__gte=start, created_at__lt=end)
        d['agent_actions_in_window'] = acts.filter(human_confirmed=False).count()
        d['human_actions_in_window'] = acts.filter(human_confirmed=True).count()
        run.detail, run.status, run.finished_at = d, 'ok', timezone.now()
        run.save(update_fields=['detail', 'status', 'finished_at'])
        closed.append(run.id)
    return closed


def _add_window_use(run, seconds):
    d = dict(run.detail)
    d['used_seconds'] = round(float(d.get('used_seconds', 0)) + seconds, 3)
    d['dry_runs'] = int(d.get('dry_runs', 0)) + 1
    run.detail = d
    run.save(update_fields=['detail'])


# ── heartbeat ─────────────────────────────────────────────────────────────────

def _beat(counts: dict, duration: float) -> None:
    now = timezone.now()
    hb, _ = Heartbeat.objects.get_or_create(name=HEARTBEAT, defaults={'last_beat': now})
    hb.last_beat = hb.last_ok_at = now
    hb.counts = counts
    hb.detail = {**(hb.detail or {}), 'duration_seconds': round(duration, 3)}
    hb.alert = duration > SLOW_CYCLE_SECONDS
    hb.alert_message = (f'agent_cycle took {duration:.0f} s (over {SLOW_CYCLE_SECONDS} s)'
                        if hb.alert else '')
    hb.save()


# ── entry point ───────────────────────────────────────────────────────────────

def run_cycle(now=None) -> dict:
    now = now or timezone.now()
    t_start = time.monotonic()
    try:
        lock = _CycleLock().__enter__()
    except CycleBusy:
        AgentRun.objects.create(kind='cycle', status='skipped', finished_at=timezone.now(),
                                detail={'outcome': 'overlap_skipped'})
        return {'outcome': 'overlap_skipped'}
    try:
        return _run(now, t_start)
    finally:
        lock.__exit__(None, None, None)


def _run(now, t_start) -> dict:
    try:
        actor = require_service_user()      # alert + stop on failure
    except ServiceUserError as exc:
        beat_error(HEARTBEAT, exc)
        return {'outcome': 'service_user_error', 'error': str(exc)}
    cfg = AgentConfig.objects.filter(pk=1).first() or AgentConfig()
    if not cfg.enabled:
        dur = time.monotonic() - t_start
        _beat({'disabled': True}, dur)
        return {'outcome': 'disabled'}

    cycle = AgentRun.objects.create(kind='cycle', status='running')
    night, w_start, w_end, in_window = night_of(now, cfg)
    shadow_ok = in_window and dry_runs_allowed()
    result = {'outcome': 'ok', 'now': now.isoformat(), 'in_window': in_window, 'shadow_allowed': shadow_ok,
              'night': night.isoformat(), 'scopes': {}, 'counts': {}}
    try:
        window_run = open_window(night, w_start, w_end) if shadow_ok else None
        scopes = [s for s in sync_scopes() if gate.is_enabled(s.account_id)]
        sel = select(scopes, now, cfg, shadow_since=w_start if shadow_ok else None)
        result.update(candidates=sel.candidates, fingerprint_seconds=sel.fingerprint_seconds,
                      fingerprint_fallback=sel.fingerprint_fallback)
        counts = {k: 0 for k in ('observed', 'debounced', 'failed', *YIELDS, 'shadow_ok', 'shadow_yielded',
                                 'shadow_skipped', 'shadow_failed')}
        done = 0
        for pick in sel.picks:
            if done >= cfg.max_scopes_per_cycle:
                result['capped'] = len(sel.picks) - done
                break
            sc = pick.scope
            from .tools.reconcile import debounce_hit
            if debounce_hit(sc):
                AgentRun.objects.create(kind='scope', scope=sc, status='skipped', finished_at=timezone.now(),
                                        detail={'mode': 'observe', 'outcome': 'debounced', 'why': pick.why})
                counts['debounced'] += 1
                result['scopes'][sc.id] = 'debounced'
                continue
            done += 1
            result['scopes'][sc.id] = _visit(sc, pick.why, now, actor, cycle, window_run, shadow_ok, cfg, counts)
        result['counts'] = counts
        result['windows_closed'] = close_windows(now)
        try:
            from .digest import maybe_send
            result['digest'] = maybe_send(now)
        except Exception as exc:                  # noqa: BLE001 — the digest never fails the cycle
            _recover()
            result['digest'] = {'error': f'{type(exc).__name__}: {exc}'[:300]}
    except CoreWriteAttempt as exc:
        # S1: stop and ask. Never caught and ignored.
        _recover()
        cycle.status, cycle.error, cycle.finished_at = 'failed', repr(exc), timezone.now()
        cycle.detail = {**result, 'outcome': 'core_write_attempt'}
        cycle.save()
        Heartbeat.objects.update_or_create(name=HEARTBEAT, defaults={
            'last_beat': timezone.now(), 'alert': True,
            'alert_message': f'STOP: write attempted inside a read-only agent transaction: {exc}'[:2000]})
        beat_error(HEARTBEAT, exc)
        raise
    dur = time.monotonic() - t_start
    result['duration_seconds'] = round(dur, 3)
    cycle.status, cycle.finished_at = 'ok', timezone.now()
    cycle.detail = to_jsonable({k: v for k, v in result.items()})
    cycle.save(update_fields=['status', 'finished_at', 'detail'])
    _beat({**result['counts'], 'scopes': len(result['scopes']), 'in_window': in_window}, dur)
    return result


def _visit(sc, why, now, actor, cycle, window_run, shadow_ok, cfg, counts) -> str:
    run = AgentRun.objects.create(kind='scope', scope=sc, status='running',
                                  detail={'mode': 'observe', 'why': why, 'cycle_id': cycle.id})
    detail = dict(run.detail)
    try:
        obs = observe_scope(sc, now, actor, run)
    except CoreWriteAttempt:
        run.status, run.finished_at = 'failed', timezone.now()
        run.detail = {**detail, 'outcome': 'core_write_attempt'}
        run.save()
        raise
    except Exception as exc:                        # noqa: BLE001
        reason = _yield_reason(exc)
        _recover()
        outcome = reason or 'failed'
        counts[outcome if reason else 'failed'] += 1
        run.status = 'skipped' if reason else 'failed'
        run.error = '' if reason else f'{type(exc).__name__}: {exc}'[:2000]
        run.detail, run.finished_at = {**detail, 'outcome': outcome}, timezone.now()
        run.save()
        log.warning('agent_cycle scope %s: %s', sc.id, outcome)
        return outcome
    counts['observed'] += 1
    detail.update(obs['detail'], outcome='ok')
    if shadow_ok and window_run is not None:
        used = float(window_run.detail.get('used_seconds', 0))
        sh = shadow_scope(sc, obs, cfg.shadow_budget_seconds - used)
        detail['shadow'] = sh
        if sh['outcome'] == 'ok':
            counts['shadow_ok'] += 1
        elif sh['outcome'] in YIELDS:
            counts['shadow_yielded'] += 1
            counts[sh['outcome']] += 1
        elif sh['outcome'] == 'failed':
            counts['shadow_failed'] += 1
        else:
            counts['shadow_skipped'] += 1
        if sh['outcome'] != 'skipped':
            _add_window_use(window_run, sh['seconds'])
    run.status, run.finished_at, run.detail = 'ok', timezone.now(), to_jsonable(detail)
    run.save(update_fields=['status', 'finished_at', 'detail'])
    return 'ok'
