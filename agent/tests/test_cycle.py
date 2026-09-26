"""Phase 3 a/c + S5, S7, S10: agent_cycle rules, one test per rule."""
import datetime
from datetime import timedelta
from unittest import mock, skipIf, skipUnless

from django.db import DatabaseError, connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from agent import cycle
from agent.db import CoreWriteAttempt, Yielded
from agent.models import (
    AgentAction, AgentConfig, AgentRun, Heartbeat, PendingEffect, ScopeLockRow, ScopeState, SummarySnapshot,
)
from agent.service import ensure_service_user
from core.models import Schedule, SummaryReportMeta, TCRow

from . import factories as f

PG = connection.vendor == 'postgresql'
TZ = timezone.get_current_timezone()


def colombo(y, m, d, hh, mm=0):
    return timezone.make_aware(datetime.datetime(y, m, d, hh, mm), TZ)


DAY = colombo(2025, 2, 10, 12)        # daytime
NIGHT = colombo(2025, 2, 10, 2)       # inside 01:00-05:00


def enable(**kw):
    AgentConfig.objects.update_or_create(pk=1, defaults={'enabled': True, **kw})


def age_uploads():
    """Move every upload time out of the debounce window."""
    old = timezone.now() - timedelta(days=2)
    Schedule.objects.update(uploaded_at=old)
    from core.models import MonitoringData, TransmissionReport
    MonitoringData.objects.update(uploaded_at=old)
    TransmissionReport.objects.update(uploaded_at=old)


class _Setup:
    def setUp(self):
        ensure_service_user()
        enable()
        self.acc, self.s = f.full_scope()
        age_uploads()

    def scope(self):
        return ScopeState.objects.get(account=self.acc, channel=self.s.channel, month=self.s.month)

    def scope_runs(self):
        return AgentRun.objects.filter(kind='scope', scope=self.scope())


class CycleBase(_Setup, TestCase):
    pass


class DaytimeTest(CycleBase):
    def test_daytime_cycle_observes_and_never_dry_runs(self):
        res = cycle.run_cycle(now=DAY)
        self.assertFalse(res['in_window'])
        self.assertEqual(res['counts']['observed'], 1)
        self.assertFalse(AgentRun.objects.filter(kind='dry_run').exists())
        self.assertFalse(SummarySnapshot.objects.filter(kind='shadow').exists())
        self.assertEqual(SummarySnapshot.objects.filter(kind='observed', schedule=self.s).count(), 1)
        run = self.scope_runs().get()
        self.assertEqual(run.status, 'ok')
        self.assertEqual(run.detail['why'], 'due')
        self.assertNotIn('shadow', run.detail)
        self.assertEqual(AgentRun.objects.filter(kind='cycle', status='ok').count(), 1)

    def test_fresh_unchanged_scope_is_not_picked_again(self):
        cycle.run_cycle(now=DAY)
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['scopes'], {})
        self.assertEqual(self.scope_runs().count(), 1)

    def test_changed_inputs_are_picked(self):
        cycle.run_cycle(now=DAY)
        f.mapping(self.acc, brand='Other', theme='Other (30)', tc='OTHER 30')   # account-wide change
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(self.scope_runs().order_by('-id').first().detail['why'], 'changed')
        self.assertEqual(res['counts']['observed'], 1)

    def test_due_by_time(self):
        cycle.run_cycle(now=DAY)
        AgentRun.objects.filter(kind='scope').update(started_at=timezone.now() - timedelta(hours=7))
        cycle.run_cycle(now=DAY)
        self.assertEqual(self.scope_runs().order_by('-id').first().detail['why'], 'due')

    def test_s10_fingerprint_fallback_is_recorded(self):
        cycle.run_cycle(now=DAY)
        f.mapping(self.acc, brand='Other', theme='Other (30)', tc='OTHER 30')
        with mock.patch.object(cycle, 'FINGERPRINT_BUDGET_SECONDS', -1):
            res = cycle.run_cycle(now=DAY)
        self.assertTrue(res['fingerprint_fallback'])

    def test_needs_run_first_and_cleared_after_observed_snapshot(self):
        sc = ScopeState.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month, needs_run=True)
        cycle.run_cycle(now=DAY)
        sc.refresh_from_db()
        self.assertFalse(sc.needs_run)
        self.assertEqual(self.scope_runs().get().detail['why'], 'needs_run')

    def test_needs_run_kept_when_observation_fails(self):
        ScopeState.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month, needs_run=True)
        with mock.patch.object(cycle, 'observe_reads', side_effect=DatabaseError('boom')):
            res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['counts']['failed'], 1)
        self.assertTrue(self.scope().needs_run)
        self.assertEqual(self.scope_runs().get().status, 'failed')
        self.assertFalse(SummarySnapshot.objects.exists())

    def test_busy_yield_is_recorded_and_not_retried_in_the_cycle(self):
        ScopeState.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month, needs_run=True)
        with mock.patch.object(cycle, 'observe_reads', side_effect=Yielded('busy_yielded')) as m:
            res = cycle.run_cycle(now=DAY)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(res['counts']['busy_yielded'], 1)
        run = self.scope_runs().get()
        self.assertEqual((run.status, run.detail['outcome']), ('skipped', 'busy_yielded'))
        self.assertTrue(self.scope().needs_run)

    def test_timeout_yield(self):
        with mock.patch.object(cycle, 'observe_reads', side_effect=Yielded('timeout_yielded')):
            res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['counts']['timeout_yielded'], 1)

    def test_core_write_attempt_stops_the_cycle(self):
        with mock.patch.object(cycle, 'observe_reads', side_effect=CoreWriteAttempt('write')):
            with self.assertRaises(CoreWriteAttempt):
                cycle.run_cycle(now=DAY)
        hb = Heartbeat.objects.get(name='agent_cycle')
        self.assertTrue(hb.alert)
        self.assertIn('STOP', hb.alert_message)
        self.assertEqual(AgentRun.objects.get(kind='cycle').status, 'failed')

    def test_debounce_skips_and_keeps_needs_run(self):
        ScopeState.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month, needs_run=True)
        Schedule.objects.update(uploaded_at=timezone.now())
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['counts']['debounced'], 1)
        self.assertEqual(self.scope_runs().get().detail['outcome'], 'debounced')
        self.assertTrue(self.scope().needs_run)
        self.assertFalse(SummarySnapshot.objects.exists())

    def test_cap(self):
        f.full_scope(self.acc, number='201', channel='Derana TV')
        age_uploads()
        enable(max_scopes_per_cycle=1)
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['counts']['observed'], 1)
        self.assertEqual(res['capped'], 1)
        res = cycle.run_cycle(now=DAY)          # the other scope next cycle
        self.assertEqual(res['counts']['observed'], 1)
        self.assertEqual(AgentRun.objects.filter(kind='scope', status='ok').values('scope').distinct().count(), 2)

    def test_kill_switch_off_writes_heartbeat_only(self):
        enable(enabled=False)
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['outcome'], 'disabled')
        self.assertFalse(AgentRun.objects.exists())
        self.assertFalse(ScopeState.objects.exists())
        self.assertEqual(Heartbeat.objects.get(name='agent_cycle').counts, {'disabled': True})

    def test_service_user_missing_stops_with_alert(self):
        from accounts.models import User
        from agent.service import service_email
        User.objects.filter(email=service_email()).delete()
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['outcome'], 'service_user_error')
        self.assertTrue(Heartbeat.objects.get(name='agent_cycle').alert)
        self.assertFalse(ScopeState.objects.exists())

    def test_cycle_never_syncs_service_user_accounts(self):
        """S8: sync_service_user writes accounts.User; the cycle only checks the user."""
        with mock.patch('agent.service.sync_service_user') as m:
            cycle.run_cycle(now=DAY)
        m.assert_not_called()
        self.assertFalse(AgentAction.objects.exists())

    def test_slow_cycle_alert(self):
        with mock.patch.object(cycle, 'SLOW_CYCLE_SECONDS', -1):
            cycle.run_cycle(now=DAY)
        hb = Heartbeat.objects.get(name='agent_cycle')
        self.assertTrue(hb.alert)
        self.assertIn('duration_seconds', hb.detail)
        cycle.run_cycle(now=DAY)
        self.assertFalse(Heartbeat.objects.get(name='agent_cycle').alert)

    def test_unexplained_v5_marks_needs_human(self):
        cycle.run_cycle(now=DAY)
        # numbers move with nothing in the fingerprint to explain them (TC flags are not in it)
        TCRow.objects.update(is_schedule_matched=True, is_lmrb_confirmed=True)
        AgentRun.objects.filter(kind='scope').update(started_at=timezone.now() - timedelta(hours=7))
        cycle.run_cycle(now=DAY)
        run = self.scope_runs().order_by('-id').first()
        self.assertEqual(run.detail['v5_unexplained'], [self.s.id])
        sc = self.scope()
        self.assertEqual((sc.state, sc.reason), ('NEEDS_HUMAN', 'unexplained_change'))


class OverlapTest(CycleBase):
    @skipIf(PG, 'SQLite: ScopeLockRow cycle lock')
    def test_second_cycle_exits_with_overlap_skipped(self):
        ScopeLockRow.objects.create(key=cycle.CYCLE_LOCK_KEY, owner='other:1', acquired_at=timezone.now(),
                                    expires_at=timezone.now() + timedelta(minutes=10))
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['outcome'], 'overlap_skipped')
        run = AgentRun.objects.get(kind='cycle')
        self.assertEqual((run.status, run.detail['outcome']), ('skipped', 'overlap_skipped'))
        self.assertFalse(AgentRun.objects.filter(kind='scope').exists())

    @skipUnless(PG, 'PostgreSQL session advisory lock')
    def test_second_cycle_exits_with_overlap_skipped_pg(self):
        other = connections.create_connection('default')
        try:
            with other.cursor() as cur:
                cur.execute('SELECT pg_advisory_lock(%s)', [cycle.CYCLE_LOCK_KEY])
            res = cycle.run_cycle(now=DAY)
            self.assertEqual(res['outcome'], 'overlap_skipped')
        finally:
            with other.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock(%s)', [cycle.CYCLE_LOCK_KEY])
            other.close()
        res = cycle.run_cycle(now=DAY)           # lock released -> runs
        self.assertEqual(res['outcome'], 'ok')


@override_settings(AGENT_ALLOW_SQLITE_DRY_RUN=True)
class WindowTest(_Setup, TransactionTestCase):
    """TransactionTestCase: on SQLite the dry run's ScopeLockRow must be taken outside atomic."""
    def test_night_of_handles_default_and_midnight_windows(self):
        cfg = AgentConfig.objects.get(pk=1)
        night, start, end, inside = cycle.night_of(NIGHT, cfg)
        self.assertEqual((night, inside), (datetime.date(2025, 2, 10), True))
        self.assertFalse(cycle.night_of(DAY, cfg)[3])
        cfg.shadow_window_start, cfg.shadow_window_end = datetime.time(23, 0), datetime.time(3, 0)
        self.assertTrue(cycle.night_of(colombo(2025, 2, 10, 23, 30), cfg)[3])
        n, s, e, i = cycle.night_of(colombo(2025, 2, 11, 2, 0), cfg)
        self.assertEqual((n, i), (datetime.date(2025, 2, 10), True))
        self.assertFalse(cycle.night_of(colombo(2025, 2, 11, 3, 0), cfg)[3])

    def test_in_window_shadow_run_stores_shadow_and_pending_effect(self):
        res = cycle.run_cycle(now=NIGHT)
        self.assertTrue(res['in_window'])
        self.assertEqual(res['counts']['shadow_ok'], 1)
        sh = SummarySnapshot.objects.get(kind='shadow')
        pe = PendingEffect.objects.get()
        self.assertEqual((pe.shadow_id, pe.observed.kind), (sh.id, 'observed'))
        self.assertEqual(AgentRun.objects.get(kind='dry_run').status, 'rolled_back')
        win = AgentRun.objects.get(kind='shadow_window')
        self.assertEqual(win.detail['dry_runs'], 1)
        self.assertIn('start', win.detail)
        self.assertEqual(win.detail['start']['label'], 'detection, not proof')
        # nothing committed to core: TC rows are untouched by the rolled-back run
        self.assertFalse(TCRow.objects.filter(is_schedule_matched=True).exists())

    def test_one_shadow_per_scope_per_night(self):
        cycle.run_cycle(now=NIGHT)
        res = cycle.run_cycle(now=NIGHT + timedelta(minutes=15))
        self.assertEqual(res['scopes'], {})
        self.assertEqual(AgentRun.objects.filter(kind='dry_run').count(), 1)

    def test_no_dry_run_when_dry_runs_not_allowed(self):
        with mock.patch.object(cycle, 'dry_runs_allowed', return_value=False):
            res = cycle.run_cycle(now=NIGHT)
        self.assertFalse(res['shadow_allowed'])
        self.assertFalse(AgentRun.objects.filter(kind__in=('dry_run', 'shadow_window')).exists())

    def test_budget_stops_dry_runs(self):
        enable(shadow_budget_seconds=0)
        res = cycle.run_cycle(now=NIGHT)
        self.assertEqual(res['counts']['shadow_skipped'], 1)
        self.assertEqual(self.scope_runs().get().detail['shadow']['reason'], 'budget_exhausted')
        self.assertFalse(AgentRun.objects.filter(kind='dry_run').exists())

    def test_s5_authorised_scope_is_observed_only(self):
        SummaryReportMeta.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month,
                                         authorised_by='Finance Head')
        cycle.run_cycle(now=NIGHT)
        run = self.scope_runs().get()
        self.assertEqual(run.detail['shadow'], {'outcome': 'skipped', 'reason': 'authorised', 'seconds': 0})
        self.assertTrue(SummarySnapshot.objects.filter(kind='observed').exists())
        self.assertFalse(AgentRun.objects.filter(kind='dry_run').exists())

    def test_s5_locked_scope_is_observed_only(self):
        Schedule.objects.filter(pk=self.s.pk).update(is_locked=True)
        cycle.run_cycle(now=NIGHT)
        self.assertEqual(self.scope_runs().get().detail['shadow']['reason'], 'schedule_locked')
        self.assertFalse(AgentRun.objects.filter(kind='dry_run').exists())

    def test_shadow_busy_yield_is_retried_by_a_later_cycle_only(self):
        with mock.patch.object(cycle, 'shadow_run', side_effect=Yielded('busy_yielded')) as m:
            res = cycle.run_cycle(now=NIGHT)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(res['counts']['shadow_yielded'], 1)
        self.assertEqual(self.scope_runs().get().detail['shadow']['outcome'], 'busy_yielded')
        res = cycle.run_cycle(now=NIGHT + timedelta(minutes=15))
        self.assertEqual(res['counts']['shadow_ok'], 1)

    def test_window_closes_with_end_fingerprint(self):
        cycle.run_cycle(now=NIGHT)
        cycle.run_cycle(now=colombo(2025, 2, 10, 5, 15))
        win = AgentRun.objects.get(kind='shadow_window')
        self.assertEqual(win.status, 'ok')
        self.assertIn('end', win.detail)
        self.assertEqual(win.detail['agent_actions_in_window'], 0)
        self.assertEqual(win.detail['diff'], {})


@override_settings(AGENT_ALLOW_SQLITE_DRY_RUN=True)
class RealTimeoutTest(TransactionTestCase):
    """PostgreSQL: a real lock held by another connection makes the shadow step yield."""

    @skipUnless(PG, 'PostgreSQL lock_timeout')
    def test_lock_held_elsewhere_gives_busy_yielded(self):
        ensure_service_user()
        enable(db_lock_timeout_ms=200)
        acc, s = f.full_scope()
        age_uploads()
        other = connections.create_connection('default')
        other.set_autocommit(False)
        try:
            with other.cursor() as cur:
                cur.execute('LOCK TABLE core_tcrow IN ACCESS EXCLUSIVE MODE')
            res = cycle.run_cycle(now=NIGHT)
        finally:
            other.rollback()
            other.close()
        self.assertEqual(res['counts']['busy_yielded'] + res['counts']['shadow_yielded'], 1)
