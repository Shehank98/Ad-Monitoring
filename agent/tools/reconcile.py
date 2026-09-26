"""
reconcile_scope and dry_run (Amendment A4, A5, P1, P2, P6).

Order (the order a person uses today), all inside ONE transaction.atomic() under
ScopeLock:
  1. run_scope(account, channel, month, 'smart') once per scope — skipped with reason
     no_commercial_rows when the scope has no COMMERCIAL BENEFITS rows (P1). Any other
     ValueError propagates.
  2. per active schedule, engine order: reconcile_tc(..., 'smart', schedule_id=sid),
     reconcile_sponsorship(..., 'smart', schedule_id=sid)
  3. reconcile_period_sponsorship(ps) for each PeriodSponsorship of the scope (by id)
  4. build_summary_data(..., schedule_id=sid) per active schedule
reconcile_tc / reconcile_sponsorship / build_summary_data are NEVER called without
schedule_id. The standalone path (reconcile_tc_lmrb) is used only when the scope has
no active schedule.

A real run is tier T1 (gate.perform). A dry run is T0: identical steps, then the
transaction is rolled back. Dry runs need PostgreSQL unless settings allow SQLite.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from core.models import MonitoringData, PeriodSponsorship, Schedule, TransmissionReport
from verification.engine import run_scope
from verification.period_sponsorship_engine import reconcile_period_sponsorship
from verification.sponsorship_engine import reconcile_sponsorship
from verification.tc_engine import _build_tc_theme_map, build_summary_data, reconcile_tc
from verification.tc_lmrb_engine import reconcile_tc_lmrb

from .. import gate, validate
from ..canonical import sha256_of, to_jsonable
from ..checks import makeup_linked_ids, multi_flag_count
from ..fingerprint import diff as fingerprint_diff, fingerprint, fingerprint_sha
from ..fingerprint import is_current as fingerprint_is_current, relevant_diff, scope_context
from ..locks import scope_lock
from ..models import (
    AgentAction, AgentAuthorisation, AgentConfig, AgentProposal, AgentRun, ScheduleStatus,
    ScopeState, SummarySnapshot,
)
from ..readiness import assess
from ..service import require_service_user
from ..scope import active_schedules, has_commercial_rows, lock_key, period


class DryRunNotAllowed(Exception):
    pass


def debounce_hit(scope: ScopeState, minutes: int | None = None) -> bool:
    """A5: skip a scope while the core upload thread may still be running."""
    if minutes is None:
        cfg = AgentConfig.objects.filter(pk=1).first()
        minutes = cfg.upload_debounce_minutes if cfg else 10
    since = timezone.now() - timedelta(minutes=minutes)
    # Phase 2.1 item 3: TC uploads (incl. an admin Confirm from the TC Inbox) count too.
    return (Schedule.objects.filter(account_id=scope.account_id, uploaded_at__gte=since).exists()
            or MonitoringData.objects.filter(account_id=scope.account_id, uploaded_at__gte=since).exists()
            or TransmissionReport.objects.filter(account_id=scope.account_id, uploaded_at__gte=since).exists())


def schedule_summaries(scope: ScopeState, active) -> dict:
    """{schedule_id: jsonable build_summary_data(schedule_id=sid)} — schedule_id always passed."""
    return {s.id: to_jsonable(build_summary_data(scope.account_id, scope.channel, scope.month,
                                                 schedule_id=s.id)) for s in active}


def engine_steps(scope: ScopeState, active) -> dict:
    """Steps 1–3 of A4. Caller must hold ScopeLock inside transaction.atomic()."""
    if not connection.in_atomic_block:
        raise RuntimeError('engine_steps must run inside transaction.atomic() under ScopeLock')
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    detail = {'run_scope': None, 'schedules': {}, 'period_sponsorships': {}}
    if has_commercial_rows(scope):
        dfs, total = run_scope(acc, ch, mo, 'smart')
        names = ('matched', 'programme_mismatch', 'late_telecast', 'not_aired', 'extra_aired')
        detail['run_scope'] = {n: int(len(df)) for n, df in zip(names, dfs)}
        detail['run_scope']['total_schedule_rows'] = int(total)
    else:
        detail['run_scope'] = {'skipped': 'no_commercial_rows'}
    for s in active:
        detail['schedules'][str(s.id)] = {
            'schedule_number': s.schedule_number,
            'reconcile_tc': reconcile_tc(acc, ch, mo, mode='smart', schedule_id=s.id),
            'reconcile_sponsorship': reconcile_sponsorship(acc, ch, mo, mode='smart', schedule_id=s.id),
        }
    for ps_id in (PeriodSponsorship.objects.filter(account_id=acc, channel=ch, month=mo)
                  .order_by('id').values_list('id', flat=True)):
        ps = PeriodSponsorship.objects.get(pk=ps_id)
        detail['period_sponsorships'][str(ps_id)] = to_jsonable(reconcile_period_sponsorship(ps))
    return detail


def _dry_run_allowed() -> bool:
    return connection.vendor == 'postgresql' or getattr(settings, 'AGENT_ALLOW_SQLITE_DRY_RUN', False)


def reconcile_scope(scope_id: int, *, actor=None, dry: bool = False, change=None,
                    respect_debounce: bool = True) -> dict:
    """[T1] Reconcile one scope (or, with dry=True, rehearse it and roll back).

    Returns a JSON-safe dict: status, per-schedule summary sha256 before/after, the
    engine counts, and the V1–V3/V5 checks. Validation failure rolls the run back and
    puts the scope in NEEDS_HUMAN.
    """
    if change is not None and not dry:
        # Guardian check 5: a hypothesis may only ever run inside a rolled-back dry run,
        # never as a committed write outside gate.perform().
        raise ValueError('change= is only allowed with dry=True')
    scope = ScopeState.objects.get(pk=scope_id)
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    if dry and not _dry_run_allowed():
        raise DryRunNotAllowed('dry_run requires PostgreSQL (set AGENT_ALLOW_SQLITE_DRY_RUN in tests only)')
    if not dry:
        gate.ensure_enabled(acc)
        if not gate.allowed(gate.T1, acc):
            raise gate.TierNotAllowed('reconcile_scope needs autonomy level >= 1')
        if actor is None:          # A9: the service user is the actor on every AgentAction
            actor = require_service_user()     # role == operations, else alert + stop

    r = assess(scope)
    if r.state == 'NEEDS_HUMAN':
        return {'status': 'skipped', 'reason': r.reason or 'needs_human', 'scope_id': scope.id}
    if r.state == 'AUTHORISED':
        # Frozen for the agent: no engine runs. Only detect changes after authorisation.
        return _authorised_check(scope, dry, actor)
    if respect_debounce and debounce_hit(scope):
        return {'status': 'skipped', 'reason': 'debounce', 'scope_id': scope.id}

    active = active_schedules(scope)
    run = AgentRun.objects.create(kind='dry_run' if dry else 'scope', scope=scope)
    result = {'scope_id': scope.id, 'dry': dry, 'status': 'ok', 'schedules': {}, 'checks': []}
    try:
        with scope_lock(lock_key(acc, ch, mo)):
            if not active:     # standalone: TC without schedule
                # V2 over the whole channel: a standalone scope has no schedule period
                locks_before = multi_flag_count(acc, ch, None, None)

                def apply_standalone():
                    return {'reconcile_tc_lmrb': reconcile_tc_lmrb(acc, ch, mo, mode='smart')}
                if dry:
                    result['standalone'] = apply_standalone()
                else:
                    gate.perform(tier=gate.T1, action_type='reconcile_standalone', scope=scope,
                                 target_model='Scope', target_pk=scope.id,
                                 before={'multi_flag_lmrb': locks_before},
                                 apply=apply_standalone, reason='smart TC↔LMRB (no schedule)',
                                 actor=actor, run=run)
                locks_after = multi_flag_count(acc, ch, None, None)
                v2 = validate.v2(locks_before, locks_after)
                result['checks'] = [{'code': v2.code, 'ok': v2.ok, 'detail': v2.detail}]
                result['multi_flag_lmrb'] = {'before': locks_before, 'after': locks_after}
                if not v2.ok:
                    result['status'] = 'validation_failed'
                if dry or not v2.ok:
                    transaction.set_rollback(True)
            else:
                _schedule_path(scope, active, result, dry, change, actor, run)
    except Exception as exc:
        run.status, run.error, run.finished_at = 'failed', repr(exc), timezone.now()
        run.save(update_fields=['status', 'error', 'finished_at'])
        raise

    # Outside the (possibly rolled-back) transaction: agent bookkeeping only.
    failed = result['status'] == 'validation_failed'
    if not dry and not failed:
        _create_amendments(scope, result.get('amendments', []), actor, run)
    run.status = 'rolled_back' if (dry or failed) else 'ok'
    run.detail = {k: v for k, v in result.items() if k not in ('schedules',)}
    run.finished_at = timezone.now()
    run.save(update_fields=['status', 'detail', 'finished_at'])
    if not dry:
        locks = result.get('multi_flag_lmrb', {})
        _update_scope_state(scope, locks.get('before') if failed else locks.get('after'),
                            failed or result['status'] == 'unexplained_change',
                            result.get('baselines'))
    return result


def _schedule_path(scope, active, result, dry, change, actor, run):
    """Schedule scopes: body of reconcile_scope inside ScopeLock/atomic."""
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    start, end = period(scope, active)
    locks_before = multi_flag_count(acc, ch, start, end) if start else 0
    before = schedule_summaries(scope, active)
    before_sha = {sid: sha256_of(d) for sid, d in before.items()}

    # V5 on the state we found (changes since our last snapshot must be explained)
    fp_now = fingerprint(scope)
    ctx = scope_context(scope)
    v5 = [validate.v5(scope, s, before_sha[s.id], fp_now, ctx) for s in active]
    # Account-wide diff kept for information; only the scope-relevant part explains.
    result['external_changes'] = {
        str(c.detail['schedule_id']): {'account_wide': c.detail['fingerprint_diff'],
                                       'relevant': c.detail.get('relevant_diff', {})}
        for c in v5 if c.detail.get('fingerprint_diff')}
    result['baselines'] = {str(c.detail['schedule_id']): c.detail['baseline']
                           for c in v5 if c.detail.get('baseline')}
    result['amendments'] = [spec for s, c in zip(active, v5)
                            if (spec := _amendment_spec(scope, s, before[s.id], c))]

    if change is not None:     # dry-run hypothesis (tests / golden check only, P6)
        change()

    box = {}

    def apply():
        box['steps'] = engine_steps(scope, active)
        box['after'] = schedule_summaries(scope, active)
        return {'summary_sha256': {str(k): sha256_of(v) for k, v in box['after'].items()},
                'summaries': {str(k): v for k, v in box['after'].items()}}

    if dry:
        apply()
    else:
        action = gate.perform(
            tier=gate.T1, action_type='reconcile_scope', scope=scope, target_model='Scope',
            target_pk=scope.id, before={'summary_sha256': {str(k): v for k, v in before_sha.items()},
                                        'summaries': {str(k): v for k, v in before.items()},
                                        'multi_flag_lmrb': locks_before},
            apply=apply, reason='smart reconcile (A4 order)', actor=actor, run=run)
        result['action_id'] = action.id

    after = box['after']
    locks_after = multi_flag_count(acc, ch, start, end) if start else 0
    tc_map = _build_tc_theme_map(acc)
    checks = [validate.v2(locks_before, locks_after), validate.v3(active)] + v5
    for s in active:
        rows = validate.v1_rows(after[s.id], acc, tc_map)
        checks += rows
        result['schedules'][str(s.id)] = {
            'schedule_number': s.schedule_number,
            'before_sha256': before_sha[s.id], 'after_sha256': sha256_of(after[s.id]),
            'before': before[s.id], 'after': after[s.id],
        }
    result['engine'] = box['steps']
    result['checks'] = [{'code': c.code, 'ok': c.ok, 'detail': c.detail} for c in checks]
    failed = any(not c.ok and c.code in ('V2', 'V3', 'VALIDATION_FAIL') for c in checks)
    unexplained = any(not c.ok for c in v5)

    if dry or failed:
        transaction.set_rollback(True)
    else:
        fp_after = fingerprint(scope)
        fp_sha = fingerprint_sha(fp_after)
        for s in active:
            SummarySnapshot.objects.create(
                scope=scope, schedule=s, schedule_number=s.schedule_number, kind='observed',
                data=after[s.id], sha256=sha256_of(after[s.id]), run=run,
                fingerprint=fp_after, fingerprint_sha256=fp_sha)
    result['multi_flag_lmrb'] = {'before': locks_before, 'after': locks_after}
    if failed:
        result['status'] = 'validation_failed'
    elif unexplained:
        result['status'] = 'unexplained_change'


def _amendment_spec(scope, schedule, current_data: dict, v5check) -> dict | None:
    """Numbers of an AUTHORISED schedule changed and the change is explained (external
    change or logged action): propose an amendment showing before and after numbers."""
    d = v5check.detail
    if not (d.get('changed') and d.get('authorised') and v5check.ok):
        return None
    auth = (AgentAuthorisation.objects.filter(schedule=schedule)
            .select_related('snapshot').order_by('-authorised_at', '-id').first())
    if auth is None or auth.snapshot_sha256 == d['new_sha']:
        return None
    return {'schedule_id': schedule.id, 'schedule_number': schedule.schedule_number,
            'authorised_sha': auth.snapshot_sha256, 'authorised_data': auth.snapshot.data,
            'current_sha': d['new_sha'], 'current_data': current_data,
            'explained_by': d['explained_by'], 'fingerprint_diff': d.get('relevant_diff', {}),
            'account_wide_diff': d['fingerprint_diff']}


def _authorised_check(scope, dry: bool, actor) -> dict:
    """AUTHORISED scope: never re-run. Compare each authorised schedule with its
    authorised snapshot; explained changes become amendment proposals, unexplained
    changes put the scope in NEEDS_HUMAN. Read-only apart from agent tables."""
    fp_now, ctx, current = _authorised_reads(scope)
    fp_sha = fingerprint_sha(fp_now)
    result = {'scope_id': scope.id, 'dry': dry, 'status': 'skipped', 'reason': 'authorised', 'amendments': [],
              'external_changes': {}, 'checks': []}
    for s, auth, data in current:
        sha = sha256_of(data)
        if sha == auth.snapshot_sha256:
            continue
        snap = auth.snapshot
        reasons, full, fp_diff = [], {}, {}
        if validate.explaining_actions(scope, snap.created_at).exists():
            reasons.append('agent_action')
        # Authorised numbers are never accepted on a baseline: a snapshot without a current
        # fingerprint cannot explain anything here (the change stays unexplained).
        if fingerprint_is_current(snap.fingerprint) and snap.fingerprint_sha256 != fp_sha:
            full = fingerprint_diff(snap.fingerprint, fp_now)
            fp_diff = relevant_diff(full, snap.fingerprint, fp_now, ctx)
            if fp_diff:
                reasons.append('external_change')
        result['checks'].append({'code': 'V5', 'ok': bool(reasons),
                                 'detail': {'schedule_id': s.id, 'explained_by': reasons,
                                            'fingerprint_diff': full, 'relevant_diff': fp_diff}})
        if full:
            result['external_changes'][str(s.id)] = {'account_wide': full, 'relevant': fp_diff}
        if reasons:
            result['amendments'].append({
                'schedule_id': s.id, 'schedule_number': s.schedule_number,
                'authorised_sha': auth.snapshot_sha256, 'authorised_data': snap.data,
                'current_sha': sha, 'current_data': data,
                'explained_by': reasons, 'fingerprint_diff': fp_diff, 'account_wide_diff': full})
        else:
            result['status'] = 'unexplained_change'
    if not dry and result['checks']:
        run = AgentRun.objects.create(kind='scope', scope=scope, status='ok',
                                      detail={k: v for k, v in result.items()}, finished_at=timezone.now())
        _create_amendments(scope, result['amendments'], actor, run)
        if result['status'] == 'unexplained_change':
            scope.state, scope.reason = 'NEEDS_HUMAN', 'unexplained_change_after_authorisation'
            scope.save(update_fields=['state', 'reason', 'updated_at'])
    return result


def _authorised_reads(scope):
    """All reads of the authorised check in ONE transaction that is always rolled back
    (REPEATABLE READ + READ ONLY on PostgreSQL), so a core run happening at the same time
    cannot give a half-updated view and a false 'unexplained change'."""
    class _Done(Exception):
        pass
    box = {}
    outermost = not connection.in_atomic_block
    try:
        with transaction.atomic():
            if connection.vendor == 'postgresql' and outermost:
                with connection.cursor() as cur:   # must be the transaction's first statement
                    cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            box['fp'] = fingerprint(scope)
            box['ctx'] = scope_context(scope)
            rows = []
            for s in active_schedules(scope):
                auth = (AgentAuthorisation.objects.filter(schedule=s).select_related('snapshot')
                        .order_by('-authorised_at', '-id').first())
                if auth is None:
                    continue
                rows.append((s, auth, to_jsonable(build_summary_data(
                    scope.account_id, scope.channel, scope.month, schedule_id=s.id))))
            box['rows'] = rows
            raise _Done
    except _Done:
        pass
    return box['fp'], box['ctx'], box['rows']


def _create_amendments(scope, specs, actor, run) -> list:
    """Open one amendment AgentProposal per (schedule, current numbers). No UI yet."""
    made = []
    for sp in specs:
        if AgentProposal.objects.filter(kind='amendment', status='open', schedule_id=sp['schedule_id'],
                                        after__sha256=sp['current_sha']).exists():
            continue
        made.append(AgentProposal.objects.create(
            kind='amendment', tier=gate.T4, action_type='amendment', scope=scope,
            schedule_id=sp['schedule_id'], target_model='Schedule', target_pk=str(sp['schedule_id']),
            before={'sha256': sp['authorised_sha'], 'summary': sp['authorised_data']},
            after={'sha256': sp['current_sha'], 'summary': sp['current_data']},
            reason=f"Numbers of authorised schedule #{sp['schedule_number']} changed after authorisation.",
            evidence={'explained_by': sp['explained_by'], 'fingerprint_diff': sp['fingerprint_diff'],
                      'account_wide_diff': sp.get('account_wide_diff', {}),
                      'run_id': run.id if run else None},
            actor=actor))
    return made


def _update_scope_state(scope: ScopeState, lock_count: int, needs_human: bool,
                        baselines: dict | None = None) -> None:
    r = assess(scope)
    scope.state = 'NEEDS_HUMAN' if needs_human else r.state
    scope.reason = 'validation' if needs_human else r.reason
    scope.lock_baseline = lock_count
    scope.needs_run = False
    scope.last_run_at = timezone.now()
    scope.save()
    linked = makeup_linked_ids([s.schedule.id for s in r.schedules])
    for s in r.schedules:
        ScheduleStatus.objects.update_or_create(
            schedule=s.schedule,
            defaults={'scope': scope, 'sub_status': s.sub_status, 'has_tc': s.has_tc,
                      'makeup_linked': s.schedule.id in linked,
                      # Phase 1.2: first run / pre-1.1 snapshot -> info, never NEEDS_HUMAN
                      'baseline_reason': (baselines or {}).get(str(s.schedule.id), ''),
                      'matched_count': s.matched, 'pending_count': s.pending})


def dry_run(scope_id: int, change=None) -> dict:
    """[T0] Rehearse reconcile_scope and roll back. PostgreSQL only (A5).
    Phase 1: called only by tests and the golden check (P6)."""
    return reconcile_scope(scope_id, dry=True, change=change, respect_debounce=False)
