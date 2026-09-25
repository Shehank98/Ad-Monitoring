"""Makeup schedules (owner decision 2, D25). Engines are real; spies only record calls."""
from unittest import mock

from django.test import TestCase, TransactionTestCase

from agent import checks
from agent.core_audit import collect
from agent.diagnose import diagnose
from agent.models import AgentConfig, ScheduleStatus, ScopeState
from agent.scope import sync_scopes
from agent.service import ensure_service_user
from agent.tools import reconcile as R
from agent.tools.reconcile import reconcile_scope

from . import factories as f


def build():
    acc = f.account()
    parent = f.schedule(acc, number='101')                               # January
    f.row(acc, parent, day=10)
    same = f.schedule(acc, number='102')                                 # makeup in the same scope
    same.parent_schedule = parent
    same.save()
    f.row(acc, same, day=20)
    feb = f.schedule(acc, number='201', month='February 2025')           # makeup in another month
    feb.parent_schedule = parent
    feb.save()
    ScheduleRowFeb = f.row(acc, feb, day=5)
    ScheduleRowFeb.month = 'February 2025'
    ScheduleRowFeb.save()
    return acc, parent, same, feb


class MakeupLoopTest(TransactionTestCase):
    def test_every_makeup_is_reconciled_in_exactly_one_scope_loop(self):
        acc, parent, same, feb = build()
        c = AgentConfig.get_solo()
        c.enabled, c.autonomy_level, c.upload_debounce_minutes = True, 1, 0
        c.save()
        ensure_service_user()
        seen = []
        real = R.reconcile_tc

        def spy(*a, **k):
            seen.append(k['schedule_id'])
            return real(*a, **k)
        with mock.patch('agent.tools.reconcile.reconcile_tc', side_effect=spy):
            for sc in sync_scopes([acc.id]):
                reconcile_scope(sc.id)
        for mk in (same, feb):
            self.assertEqual(seen.count(mk.id), 1, f'makeup #{mk.schedule_number} reconciled {seen.count(mk.id)} times')
        self.assertEqual(seen.count(parent.id), 1)
        self.assertNotIn(None, seen)                                       # never schedule_id=None
        self.assertEqual(checks.makeups_never_reconciled(acc.id), [])
        for s in (parent, same, feb):
            self.assertTrue(ScheduleStatus.objects.get(schedule=s).makeup_linked)


class MakeupFindingsTest(TestCase):
    def test_makeup_linked_finding_on_parent_and_makeup_scopes(self):
        acc, parent, same, feb = build()
        jan = ScopeState.objects.create(account=acc, channel=f.CHANNEL, month=f.MONTH)
        febs = ScopeState.objects.create(account=acc, channel=f.CHANNEL, month='February 2025')
        for sc in (jan, febs):
            found = [x for x in diagnose(sc) if x.code == 'MAKEUP_LINKED']
            self.assertEqual(len(found), 1, sc.month)
            ev = found[0].evidence
            self.assertEqual(ev['parent_number'], '101')
            self.assertEqual({m['makeup_number'] for m in ev['makeups']}, {'102', '201'})
            self.assertEqual({m['scope']['month'] for m in ev['makeups']}, {'January 2025', 'February 2025'})
            self.assertEqual(ev['parent_report_rows'], 1)
            self.assertTrue(all(m['report_rows'] == 1 for m in ev['makeups']))

    def test_superseded_makeup_is_reported_as_never_reconciled(self):
        acc, parent, same, feb = build()
        newer = f.schedule(acc, number='201', version=2, month='February 2025')
        self.assertEqual(checks.makeups_never_reconciled(acc.id), [feb.id])
        self.assertNotIn(newer.id, checks.makeups_never_reconciled(acc.id))

    def test_audit_counts(self):
        build()
        s = collect()['sections']
        self.assertEqual(len(s['makeup_linked']), 1)
        self.assertEqual(s['makeup_linked'][0]['count'], 2)
        self.assertEqual(s['makeup_never_reconciled'], [])
