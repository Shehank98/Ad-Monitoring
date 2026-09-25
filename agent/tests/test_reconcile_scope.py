"""reconcile_scope / dry_run integration tests (Amendment A4, A5, P1, P2, P6).

Engines are NEVER mocked: mock.patch(..., wraps=real) only records the calls.
TransactionTestCase because the non-PostgreSQL ScopeLock must be taken outside an
atomic block (it commits a ScopeLockRow)."""
from unittest import mock

from django.test import TransactionTestCase, override_settings

from agent import gate
from agent.models import AgentAction, AgentConfig, AgentRun, ScheduleStatus, ScopeState, SummarySnapshot
from agent.tools import reconcile as R
from agent.tools.reconcile import DryRunNotAllowed, dry_run, reconcile_scope
from core.models import LMRBRow, MatchResult, ScheduleRow, SummaryReportMeta, TCRow

from . import factories as f

E = 'agent.tools.reconcile.'


def enable(level=1):
    c = AgentConfig.get_solo()
    c.enabled, c.autonomy_level, c.upload_debounce_minutes = True, level, 0
    c.save()


def scope(acc):
    return ScopeState.objects.get_or_create(account=acc, channel=f.CHANNEL, month=f.MONTH)[0]


class KillSwitchTest(TransactionTestCase):
    def test_disabled_agent_writes_nothing(self):
        acc, _ = f.full_scope()
        sc = scope(acc)
        with self.assertRaises(gate.AgentDisabled):
            reconcile_scope(sc.id)
        self.assertFalse(ScheduleRow.objects.filter(is_matched=True).exists())
        self.assertFalse(AgentAction.objects.exists())

    def test_level_zero_is_read_only(self):
        acc, _ = f.full_scope()
        enable(level=0)
        with self.assertRaises(gate.TierNotAllowed):
            reconcile_scope(scope(acc).id)
        self.assertFalse(ScheduleRow.objects.filter(is_matched=True).exists())


class ReconcileScopeTest(TransactionTestCase):
    def setUp(self):
        enable(level=1)

    def test_change_hook_refused_outside_dry_run(self):
        acc, _ = f.full_scope()
        called = []
        with self.assertRaises(ValueError):
            reconcile_scope(scope(acc).id, change=lambda: called.append(1))
        self.assertEqual(called, [])
        self.assertFalse(ScheduleRow.objects.filter(is_matched=True).exists())
        self.assertFalse(AgentRun.objects.exists())

    def test_engine_order_and_schedule_id_always_passed(self):
        acc, s1 = f.full_scope(number='101')
        s2 = f.schedule(acc, number='102')
        f.row(acc, s2, day=14, start='21:00:00', end='21:15:00')
        rep2 = f.tc_report(acc, s2, rows=1)
        f.lmrb(acc, day=14, time='21:05:00')
        f.tc_row(acc, rep2, day=14, time='21:05:01')
        calls = []

        def spy(name, real):
            def w(*a, **k):
                calls.append((name, k.get('schedule_id', 'n/a')))
                return real(*a, **k)
            return w
        with mock.patch(E + 'run_scope', side_effect=spy('run_scope', R.run_scope)), \
             mock.patch(E + 'reconcile_tc', side_effect=spy('reconcile_tc', R.reconcile_tc)), \
             mock.patch(E + 'reconcile_sponsorship', side_effect=spy('reconcile_sponsorship', R.reconcile_sponsorship)), \
             mock.patch(E + 'build_summary_data', side_effect=spy('build_summary_data', R.build_summary_data)):
            res = reconcile_scope(scope(acc).id)
        self.assertEqual(res['status'], 'ok', res.get('checks'))
        engine = [c for c in calls if c[0] != 'build_summary_data']
        self.assertEqual(engine, [('run_scope', 'n/a'),
                                  ('reconcile_tc', s1.id), ('reconcile_sponsorship', s1.id),
                                  ('reconcile_tc', s2.id), ('reconcile_sponsorship', s2.id)])
        for name, sid in calls:
            if name != 'run_scope':
                self.assertIn(sid, (s1.id, s2.id), f'{name} called without schedule_id')
        self.assertEqual(SummarySnapshot.objects.filter(kind='draft').count(), 2)   # one per schedule
        self.assertEqual(AgentAction.objects.get().action_type, 'reconcile_scope')
        self.assertEqual(ScheduleStatus.objects.count(), 2)

    def test_two_schedules_share_pool_no_double_claim(self):
        acc = f.account()
        a = f.schedule(acc, number='101')
        b = f.schedule(acc, number='102')
        ra = f.row(acc, a, day=10)
        rb = f.row(acc, b, day=10)
        f.mapping(acc)
        only = f.lmrb(acc, day=10)
        f.lmrb(acc, day=31, time='23:00:00', theme='Other')
        f.tc_report(acc, a)
        f.tc_report(acc, b)
        reconcile_scope(scope(acc).id)
        only.refresh_from_db()
        self.assertEqual(only.matched_schedule_id, ra.id)            # Rule 10: #101 first
        self.assertEqual(ScheduleRow.objects.filter(is_matched=True).count(), 1)
        rb.refresh_from_db()
        self.assertFalse(rb.is_matched)

    def test_superseded_version_is_ignored(self):
        acc = f.account()
        v1 = f.schedule(acc, number='101', version=1, superseded=True)
        old = f.row(acc, v1, day=10)
        v2 = f.schedule(acc, number='101', version=2)
        new = f.row(acc, v2, day=10)
        f.mapping(acc)
        f.lmrb(acc, day=10)
        f.lmrb(acc, day=31, time='23:00:00', theme='Other')
        f.tc_report(acc, v2)
        res = reconcile_scope(scope(acc).id)
        new.refresh_from_db(); old.refresh_from_db()
        self.assertTrue(new.is_matched)
        self.assertFalse(old.is_matched)
        self.assertEqual(list(res['schedules']), [str(v2.id)])

    def test_no_commercial_rows_skips_run_scope(self):
        acc = f.account()
        s = f.schedule(acc)
        f.row(acc, s, ad_type='SPONSORSHIP')
        f.lmrb(acc, day=31)
        f.tc_report(acc, s)
        with mock.patch(E + 'run_scope', wraps=R.run_scope) as rs:
            res = reconcile_scope(scope(acc).id)
        rs.assert_not_called()
        self.assertEqual(res['engine']['run_scope'], {'skipped': 'no_commercial_rows'})

    def test_other_value_error_propagates_and_rolls_back(self):
        acc, _ = f.full_scope()
        with mock.patch(E + 'reconcile_tc', side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                reconcile_scope(scope(acc).id)
        self.assertFalse(ScheduleRow.objects.filter(is_matched=True).exists())   # run_scope rolled back
        self.assertFalse(MatchResult.objects.exists())
        self.assertEqual(AgentRun.objects.get().status, 'failed')

    def test_unmapped_brand_never_credited(self):
        acc = f.account()
        s = f.schedule(acc)
        f.row(acc, s, brand='Krest', day=10)
        f.mapping(acc, brand='Krest', theme='Krest (20)', tc='')        # LMRB theme only, no tc_theme
        f.lmrb(acc, day=10, theme='Krest (20)')
        f.lmrb(acc, day=31, time='23:00:00', theme='Other')
        rep = f.tc_report(acc, s)
        f.tc_row(acc, rep, day=10, theme='KREST 30')
        res = reconcile_scope(scope(acc).id)
        row = res['schedules'][str(s.id)]['after']['commercial'][0]
        self.assertEqual((row['aired'], row['third_party'], row['missed']), (0, 0, 1))
        self.assertIn('NO_TC_MAPPING', [c['code'] for c in res['checks']])

    def test_channel_and_month_unchanged_byte_for_byte(self):
        acc, s = f.full_scope()
        reconcile_scope(scope(acc).id)
        sc = ScopeState.objects.get()
        for obj in [sc] + list(TCRow.objects.all()):
            self.assertEqual(obj.channel.encode(), s.channel.encode())
        self.assertEqual(sc.month.encode(), s.month.encode())

    def test_authorised_scope_is_frozen(self):
        acc, _ = f.full_scope()
        SummaryReportMeta.objects.create(account=acc, channel=f.CHANNEL, month=f.MONTH, authorised_by='K')
        res = reconcile_scope(scope(acc).id)
        self.assertEqual(res['status'], 'skipped')
        self.assertFalse(ScheduleRow.objects.filter(is_matched=True).exists())

    def test_debounce(self):
        acc, _ = f.full_scope()
        c = AgentConfig.get_solo()
        c.upload_debounce_minutes = 10
        c.save()
        self.assertEqual(reconcile_scope(scope(acc).id)['reason'], 'debounce')

    def test_lock_contention_skips_nothing_silently(self):
        from agent.locks import ScopeBusy, fallback_lock
        from agent.scope import lock_key
        acc, _ = f.full_scope()
        if R.connection.vendor == 'postgresql':
            self.skipTest('fallback lock only')
        with fallback_lock(lock_key(acc.id, f.CHANNEL, f.MONTH)):
            with self.assertRaises(ScopeBusy):
                reconcile_scope(scope(acc).id)
        self.assertFalse(ScheduleRow.objects.filter(is_matched=True).exists())


class DryRunTest(TransactionTestCase):
    def test_refused_on_sqlite_without_setting(self):
        acc, _ = f.full_scope()
        if R.connection.vendor == 'postgresql':
            self.skipTest('PostgreSQL allows dry runs')
        with self.assertRaises(DryRunNotAllowed):
            dry_run(scope(acc).id)

    @override_settings(AGENT_ALLOW_SQLITE_DRY_RUN=True)
    def test_dry_run_rolls_back_and_works_while_disabled(self):
        acc, _ = f.full_scope()
        res = dry_run(scope(acc).id)
        self.assertEqual(res['status'], 'ok', res['checks'])
        self.assertTrue(res['dry'])
        (sched,) = res['schedules'].values()
        self.assertEqual(sched['after']['commercial'][0]['third_party'], 2)
        self.assertFalse(ScheduleRow.objects.filter(is_matched=True).exists())
        self.assertFalse(TCRow.objects.filter(is_lmrb_confirmed=True).exists())
        self.assertFalse(SummarySnapshot.objects.exists())
        self.assertFalse(AgentAction.objects.exists())
        self.assertEqual(AgentRun.objects.get().status, 'rolled_back')

    @override_settings(AGENT_ALLOW_SQLITE_DRY_RUN=True)
    def test_dry_run_with_change_hypothesis(self):
        acc = f.account()
        s = f.schedule(acc)
        f.row(acc, s, brand='Krest', day=10)
        f.lmrb(acc, day=10, theme='Krest (30)')
        f.lmrb(acc, day=31, time='23:00:00', theme='Other')
        rep = f.tc_report(acc, s)
        f.tc_row(acc, rep, day=10, theme='KREST 30')
        res = dry_run(scope(acc).id, change=lambda: f.mapping(acc, brand='Krest', theme='Krest (30)', tc='KREST 30'))
        (sched,) = res['schedules'].values()
        self.assertEqual(sched['after']['commercial'][0]['third_party'], 1)
        from core.models import BrandMapping
        self.assertFalse(BrandMapping.objects.exists())            # hypothesis rolled back
