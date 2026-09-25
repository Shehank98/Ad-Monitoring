import datetime

from django.test import TestCase

from agent.models import ScopeState
from agent.readiness import assess
from agent.scope import active_schedules, has_commercial_rows, makeup_schedules, sync_scopes

from . import factories as f

TODAY = datetime.date(2025, 3, 1)


def scope_of(acc, channel=f.CHANNEL, month=f.MONTH):
    return ScopeState.objects.get_or_create(account=acc, channel=channel, month=month)[0]


class ScopeTest(TestCase):
    def test_sync_copies_exact_strings(self):
        acc = f.account()
        s = f.schedule(acc, channel='TV - Sirasa TV ', month='January 2025')
        (sc,) = sync_scopes([acc.id])
        self.assertEqual(sc.channel.encode(), s.channel.encode())
        self.assertEqual(sc.month.encode(), s.month.encode())

    def test_superseded_version_is_ignored(self):
        acc = f.account()
        v1 = f.schedule(acc, number='101', version=1, superseded=True)
        v2 = f.schedule(acc, number='101', version=2)
        self.assertEqual([s.id for s in active_schedules(scope_of(acc))], [v2.id])
        self.assertNotIn(v1.id, [s.id for s in active_schedules(scope_of(acc))])

    def test_engine_order_and_makeup(self):
        acc = f.account()
        b = f.schedule(acc, number='102')
        a = f.schedule(acc, number='101')
        mk = f.schedule(acc, number='201', month='February 2025')
        mk.parent_schedule = a
        mk.save()
        sc = scope_of(acc)
        self.assertEqual([s.id for s in active_schedules(sc)], [a.id, b.id])
        self.assertEqual([s.id for s in makeup_schedules(sc)], [mk.id])

    def test_has_commercial_rows(self):
        acc = f.account()
        s = f.schedule(acc)
        f.row(acc, s, ad_type='SPONSORSHIP')
        self.assertFalse(has_commercial_rows(scope_of(acc)))
        f.row(acc, s)
        self.assertTrue(has_commercial_rows(scope_of(acc)))


class ReadinessTest(TestCase):
    def test_rule8_rows_are_waiting_not_failure(self):
        acc = f.account()
        s = f.schedule(acc)
        f.row(acc, s, day=25)
        f.mapping(acc)
        f.lmrb(acc, day=20)             # LMRB stops before the row's date
        r = assess(scope_of(acc), today=TODAY)
        self.assertEqual((r.state, r.reason), ('WAITING_INPUTS', 'lmrb_partial'))
        self.assertEqual(r.schedules[0].sub_status, 'pending_rows')
        self.assertNotEqual(r.state, 'NEEDS_HUMAN')

    def test_duplicate_active_number_needs_human(self):
        acc = f.account()
        f.schedule(acc, number='101', version=1)
        f.schedule(acc, number='101', version=2)       # keep_both: neither superseded
        r = assess(scope_of(acc), today=TODAY)
        self.assertEqual((r.state, r.reason), ('NEEDS_HUMAN', 'duplicate_active_number'))

    def test_locked_schedule_freezes_scope(self):
        acc = f.account()
        f.schedule(acc, locked=True)
        r = assess(scope_of(acc), today=TODAY)
        self.assertEqual((r.state, r.reason), ('NEEDS_HUMAN', 'schedule_locked'))

    def test_mixed_width_is_a_warning(self):
        acc = f.account()
        f.schedule(acc, number='99')
        f.schedule(acc, number='101')
        r = assess(scope_of(acc), today=TODAY)
        self.assertIn('SCHEDULE_NUMBER_WIDTH', [c for c, _ in r.warnings])
        self.assertNotEqual(r.state, 'NEEDS_HUMAN')

    def test_unmapped_brand_is_mapping(self):
        acc = f.account()
        s = f.schedule(acc)
        f.row(acc, s)
        f.lmrb(acc, day=31)
        f.tc_report(acc, s)
        r = assess(scope_of(acc), today=TODAY)
        self.assertEqual(r.state, 'MAPPING')

    def test_standalone(self):
        acc = f.account()
        f.tc_report(acc, None)
        sc = ScopeState.objects.create(account=acc, channel=f.CHANNEL, month=f.MONTH)
        self.assertEqual(assess(sc, today=TODAY).state, 'STANDALONE')
