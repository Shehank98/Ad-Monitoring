"""Golden check on SYNTHETIC scopes, including a revised schedule and a multi-schedule scope."""
import json
import os
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TransactionTestCase, override_settings

from agent import golden
from core.models import LMRBRow, MatchResult, ScheduleRow, TCRow
from verification.engine import run_scope
from verification.sponsorship_engine import reconcile_sponsorship
from verification.tc_engine import reconcile_tc

from . import factories as f


def reconcile_like_a_person(acc, channel, month, schedules):
    run_scope(acc.id, channel, month, 'smart')
    for s in schedules:
        reconcile_tc(acc.id, channel, month, mode='smart', schedule_id=s.id)
        reconcile_sponsorship(acc.id, channel, month, mode='smart', schedule_id=s.id)


def state():
    return (list(ScheduleRow.objects.order_by('id').values_list('id', 'is_matched')),
            list(TCRow.objects.order_by('id').values_list('id', 'is_schedule_matched', 'is_lmrb_confirmed')),
            MatchResult.objects.count())


@override_settings(AGENT_GOLDEN_ALLOW_SQLITE=True)
class GoldenCheckTest(TransactionTestCase):
    def setUp(self):
        # Scope 1: a revised schedule (v1 superseded by v2)
        self.a1 = f.account('Keells')
        v1 = f.schedule(self.a1, number='101', version=1, superseded=True)
        f.row(self.a1, v1, day=9)
        self.v2 = f.schedule(self.a1, number='101', version=2)
        for d in (10, 12):
            f.row(self.a1, self.v2, day=d)
            f.lmrb(self.a1, day=d)
        f.lmrb(self.a1, day=31, time='23:00:00', theme='Other')
        f.mapping(self.a1)
        rep = f.tc_report(self.a1, self.v2)
        for d in (10, 12):
            f.tc_row(self.a1, rep, day=d)
        # Scope 2: several schedules in one scope
        self.a2 = f.account('Dialog')
        self.s1 = f.schedule(self.a2, number='201')
        self.s2 = f.schedule(self.a2, number='202')
        f.mapping(self.a2, brand='Fibre', theme='Fibre (30)', tc='FIBRE 30')
        for s, d, t in ((self.s1, 10, '20:05:00'), (self.s2, 11, '20:06:00')):
            f.row(self.a2, s, brand='Fibre', day=d)
            f.lmrb(self.a2, day=d, time=t, theme='Fibre (30)')
            rep = f.tc_report(self.a2, s)
            f.tc_row(self.a2, rep, day=d, time=t, theme='FIBRE 30')
        f.lmrb(self.a2, day=31, time='23:00:00', theme='Other')
        reconcile_like_a_person(self.a1, f.CHANNEL, f.MONTH, [self.v2])
        reconcile_like_a_person(self.a2, f.CHANNEL, f.MONTH, [self.s1, self.s2])
        self.entries = [{'account': 'Keells', 'channel': f.CHANNEL, 'month': f.MONTH},
                        {'account': self.a2.id, 'channel': f.CHANNEL, 'month': f.MONTH}]

    def test_idempotent_matches_and_changes_nothing(self):
        before = state()
        rep = golden.verify_idempotent(self.entries)
        self.assertTrue(rep['ok'], golden.render(rep))
        self.assertEqual([len(s['schedules']) for s in rep['scopes']], [1, 2])   # v2 only; both of scope 2
        self.assertEqual(before, state())

    def test_idempotent_detects_a_difference(self):
        with mock.patch('agent.golden.engine_steps', side_effect=lambda sc, active: ScheduleRow.objects
                        .filter(schedule__in=active).update(brand='Changed')):
            rep = golden.verify_idempotent(self.entries[:1])
        self.assertFalse(rep['ok'])
        self.assertEqual(ScheduleRow.objects.filter(brand='Changed').count(), 0)   # rolled back

    def test_snapshot_and_rebuild_gated(self):
        snap = golden.snapshot(self.entries)
        self.assertEqual(len(snap['scopes']), 2)
        with self.assertRaises(golden.GoldenRefused):
            golden.verify_rebuild(self.entries, snap)
        before = state()
        with mock.patch.dict(os.environ, {'AGENT_DISPOSABLE_DB': '1'}):
            rep = golden.verify_rebuild(self.entries, snap)
        self.assertEqual(rep['differences'], 0, golden.render(rep))
        self.assertEqual(before, state())

    def test_scope_strings_must_match_stored_exactly(self):
        with self.assertRaises(golden.GoldenRefused):
            golden.resolve([{'account': 'Keells', 'channel': 'sirasa tv', 'month': f.MONTH}])

    @override_settings(AGENT_GOLDEN_ALLOW_SQLITE=False)
    def test_refuses_sqlite_outside_tests(self):
        from django.db import connection
        if connection.vendor == 'postgresql':
            self.skipTest('running on PostgreSQL')
        with self.assertRaises(golden.GoldenRefused):
            golden.verify_idempotent(self.entries)

    def test_command(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'scopes.json')
            with open(path, 'w') as fh:
                json.dump(self.entries, fh)
            out = StringIO()
            call_command('agent_golden_check', 'verify', '--mode', 'idempotent', '--scopes', path, stdout=out)
            self.assertIn('Result: MATCH', out.getvalue())
            with self.assertRaises(CommandError):
                call_command('agent_golden_check', 'verify', '--mode', 'rebuild', '--scopes', path, stdout=StringIO())
