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

from core.models import MonitoringData, PeriodSponsorship, Schedule
from verification.engine import run_scope
from verification.period_sponsorship_engine import reconcile_period_sponsorship
from verification.sponsorship_engine import reconcile_sponsorship
from verification.tc_engine import _build_tc_theme_map, build_summary_data, reconcile_tc
from verification.tc_lmrb_engine import reconcile_tc_lmrb

from .. import gate, validate
from ..canonical import sha256_of, to_jsonable
from ..checks import multi_flag_count
from ..locks import scope_lock
from ..models import AgentConfig, AgentRun, ScheduleStatus, ScopeState, SummarySnapshot
from ..readiness import assess
from ..scope import active_schedules, has_commercial_rows, lock_key, period


class DryRunNotAllowed(Exception):
    pass


def debounce_hit(scope: ScopeState, minutes: int | None = None) -> bool:
    """A5: skip a scope while the core upload thread may still be running."""
    if minutes is None:
        cfg = AgentConfig.objects.filter(pk=1).first()
        minutes = cfg.upload_debounce_minutes if cfg else 10
    since = timezone.now() - timedelta(minutes=minutes)
    return (Schedule.objects.filter(account_id=scope.account_id, uploaded_at__gte=since).exists()
            or MonitoringData.objects.filter(account_id=scope.account_id, uploaded_at__gte=since).exists())


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

    r = assess(scope)
    if r.state in ('AUTHORISED', 'NEEDS_HUMAN'):
        return {'status': 'skipped', 'reason': r.reason or r.state.lower(), 'scope_id': scope.id}
    if respect_debounce and debounce_hit(scope):
        return {'status': 'skipped', 'reason': 'debounce', 'scope_id': scope.id}

    active = active_schedules(scope)
    run = AgentRun.objects.create(kind='dry_run' if dry else 'scope', scope=scope)
    result = {'scope_id': scope.id, 'dry': dry, 'status': 'ok', 'schedules': {}, 'checks': []}
    try:
        with scope_lock(lock_key(acc, ch, mo)):
            if not active:     # standalone: TC without schedule
                def apply_standalone():
                    return {'reconcile_tc_lmrb': reconcile_tc_lmrb(acc, ch, mo, mode='smart')}
                if dry:
                    result['standalone'] = apply_standalone()
                    transaction.set_rollback(True)
                else:
                    gate.perform(tier=gate.T1, action_type='reconcile_standalone', scope=scope,
                                 target_model='Scope', target_pk=scope.id, before={},
                                 apply=apply_standalone, reason='smart TC↔LMRB (no schedule)',
                                 actor=actor, run=run)
            else:
                _schedule_path(scope, active, result, dry, change, actor, run)
    except Exception as exc:
        run.status, run.error, run.finished_at = 'failed', repr(exc), timezone.now()
        run.save(update_fields=['status', 'error', 'finished_at'])
        raise

    # Outside the (possibly rolled-back) transaction: agent bookkeeping only.
    failed = result['status'] == 'validation_failed'
    run.status = 'rolled_back' if (dry or failed) else 'ok'
    run.detail = {k: v for k, v in result.items() if k not in ('schedules',)}
    run.finished_at = timezone.now()
    run.save(update_fields=['status', 'detail', 'finished_at'])
    if not dry:
        locks = result.get('multi_flag_lmrb', {})
        _update_scope_state(scope, locks.get('before') if failed else locks.get('after'),
                            failed or result['status'] == 'unexplained_change')
    return result


def _schedule_path(scope, active, result, dry, change, actor, run):
    """Schedule scopes: body of reconcile_scope inside ScopeLock/atomic."""
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    start, end = period(scope, active)
    locks_before = multi_flag_count(acc, ch, start, end) if start else 0
    before = schedule_summaries(scope, active)
    before_sha = {sid: sha256_of(d) for sid, d in before.items()}

    # V5 on the state we found (changes since our last snapshot must be explained)
    v5 = [validate.v5(scope, s, before_sha[s.id]) for s in active]

    if change is not None:     # dry-run hypothesis (tests / golden check only, P6)
        change()

    box = {}

    def apply():
        box['steps'] = engine_steps(scope, active)
        box['after'] = schedule_summaries(scope, active)
        return {'summary_sha256': {str(k): sha256_of(v) for k, v in box['after'].items()}}

    if dry:
        apply()
    else:
        action = gate.perform(
            tier=gate.T1, action_type='reconcile_scope', scope=scope, target_model='Scope',
            target_pk=scope.id, before={'summary_sha256': {str(k): v for k, v in before_sha.items()},
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
        for s in active:
            SummarySnapshot.objects.create(
                scope=scope, schedule=s, schedule_number=s.schedule_number, kind='draft',
                data=after[s.id], sha256=sha256_of(after[s.id]), run=run)
    result['multi_flag_lmrb'] = {'before': locks_before, 'after': locks_after}
    if failed:
        result['status'] = 'validation_failed'
    elif unexplained:
        result['status'] = 'unexplained_change'


def _update_scope_state(scope: ScopeState, lock_count: int, needs_human: bool) -> None:
    r = assess(scope)
    scope.state = 'NEEDS_HUMAN' if needs_human else r.state
    scope.reason = 'validation' if needs_human else r.reason
    scope.lock_baseline = lock_count
    scope.needs_run = False
    scope.last_run_at = timezone.now()
    scope.save()
    for s in r.schedules:
        ScheduleStatus.objects.update_or_create(
            schedule=s.schedule,
            defaults={'scope': scope, 'sub_status': s.sub_status, 'has_tc': s.has_tc,
                      'matched_count': s.matched, 'pending_count': s.pending})


def dry_run(scope_id: int, change=None) -> dict:
    """[T0] Rehearse reconcile_scope and roll back. PostgreSQL only (A5).
    Phase 1: called only by tests and the golden check (P6)."""
    return reconcile_scope(scope_id, dry=True, change=change, respect_debounce=False)
