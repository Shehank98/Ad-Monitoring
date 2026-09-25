import datetime

from django.test import TestCase

from agent.core_audit import collect, render_markdown
from agent.diagnose import diagnose
from agent.models import ScopeState
from core.models import (
    LMRBRow, ManualMatch, MatchResult, Schedule, ScheduleRow, TCRow,
)

from . import factories as f


def codes(findings):
    return {(x.code, x.sub_code) for x in findings}


class DiagnoseTest(TestCase):
    def setUp(self):
        self.acc, self.s = f.full_scope()
        self.sc = ScopeState.objects.create(account=self.acc, channel=f.CHANNEL, month=f.MONTH)

    def test_clean_scope_has_no_bad_findings(self):
        bad = [x for x in diagnose(self.sc) if x.severity == 'bad']
        self.assertEqual(bad, [])

    def test_no_tc_mapping(self):
        f.row(self.acc, self.s, brand='Krest')
        self.assertIn(('NO_TC_MAPPING', ''), codes(diagnose(self.sc)))

    def test_wildcard_commercial(self):
        f.row(self.acc, self.s, brand='Krest')
        f.mapping(self.acc, brand='Krest', theme='Krest', tc='KREST*')
        self.assertIn(('WILDCARD_TC_THEME_COMMERCIAL', ''), codes(diagnose(self.sc)))

    def test_channel_variant(self):
        f.tc_report(self.acc, None, channel='sirasa tv')
        self.assertIn(('CHANNEL_VARIANT', ''), codes(diagnose(self.sc)))

    def test_schedule_set_findings(self):
        f.schedule(self.acc, number='101', version=2)             # duplicate active number
        f.schedule(self.acc, number='99', locked=True)
        c = codes(diagnose(self.sc))
        self.assertIn(('DUPLICATE_ACTIVE_NUMBER', ''), c)
        self.assertIn(('SCHEDULE_LOCKED', ''), c)
        self.assertIn(('SCHEDULE_NUMBER_WIDTH', ''), c)
        self.assertIn(('SUPERSEDED_ROWS_PRESENT', ''), c)

    def test_manual_lock_lost(self):
        sr = ScheduleRow.objects.filter(schedule=self.s).first()
        lr = LMRBRow.objects.filter(account=self.acc).first()
        ManualMatch.objects.create(account=self.acc, channel=f.CHANNEL, month=f.MONTH, schedule_row=sr, lmrb_row=lr)
        self.assertIn(('MANUAL_LOCK_LOST', ''), codes(diagnose(self.sc)))

    def test_time_belt_unattributed(self):
        rep = f.tc_report(self.acc, self.s)
        f.tc_row(self.acc, rep, day=15, theme='UNKNOWN SPOT', is_schedule_matched=True)
        self.assertIn(('TIME_BELT_UNATTRIBUTED', ''), codes(diagnose(self.sc)))

    def test_lock_orphaned_each_sub_code(self):
        sr = ScheduleRow.objects.filter(schedule=self.s).first()
        f.lmrb(self.acc, day=20, time='10:00:00', is_tc_lmrb_matched=True)
        f.lmrb(self.acc, day=20, time='11:00:00', is_sponsorship_matched=True)
        f.lmrb(self.acc, day=20, time='12:00:00', is_manual_matched=True)
        f.lmrb(self.acc, day=20, time='13:00:00', is_matched=True)
        ScheduleRow.objects.filter(pk=sr.pk).update(is_matched=True, matched_lmrb=None)
        c = codes(diagnose(self.sc))
        for sub in ('TC_LMRB', 'SPONSORSHIP', 'MANUAL', 'COMMERCIAL'):
            self.assertIn(('LOCK_ORPHANED', sub), c)

    def test_diagnose_writes_nothing(self):
        before = [m.objects.count() for m in (Schedule, ScheduleRow, LMRBRow, TCRow, MatchResult)]
        diagnose(self.sc)
        self.assertEqual(before, [m.objects.count() for m in (Schedule, ScheduleRow, LMRBRow, TCRow, MatchResult)])


class CoreAuditTest(TestCase):
    def test_audit_is_read_only_and_finds_issues(self):
        acc, s = f.full_scope()
        f.row(acc, s, brand='Krest')
        f.mapping(acc, brand='Krest', theme='Krest', tc='KREST*')
        f.schedule(acc, number='101', version=2)
        f.lmrb(acc, day=20, time='13:00:00', is_matched=True)
        f.lmrb(acc, day=21, time='13:00:00', is_matched=True, is_sponsorship_matched=True)
        models = (Schedule, ScheduleRow, LMRBRow, TCRow, MatchResult)
        before = [m.objects.count() for m in models]
        data = collect()
        self.assertEqual(before, [m.objects.count() for m in models])
        s_ = data['sections']
        self.assertEqual(len(s_['wildcard_tc_theme_commercial']), 1)
        self.assertEqual(len(s_['duplicate_active_numbers']), 1)
        self.assertTrue(s_['lmrb_multi_flag'])
        self.assertTrue([r for r in s_['lock_orphaned'] if r['sub_code'] == 'COMMERCIAL'])
        md = render_markdown(data, synthetic=True)
        self.assertTrue(md.startswith('SYNTHETIC DATA'))
        self.assertIn('# SYNTHETIC DATA', md)


class CoreAuditSaveTest(TestCase):
    """Guardian check 17 / owner decision 4: one final write to agent tables, after the
    read-only transaction has rolled back."""

    def test_command_saves_one_audit_run_after_collect(self):
        import tempfile
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        from agent.management.commands import agent_core_audit as cmd
        from agent.models import AgentRun
        acc, s = f.full_scope()
        f.schedule(acc, number='101', version=2)
        collect()
        self.assertFalse(AgentRun.objects.exists())           # collect() alone never writes
        order = []
        real_collect, real_save = cmd.collect, cmd.save_result
        with mock.patch.object(cmd, 'collect', side_effect=lambda: order.append('collect') or real_collect()), \
                mock.patch.object(cmd, 'save_result',
                                  side_effect=lambda *a: order.append('save') or real_save(*a)), \
                tempfile.TemporaryDirectory() as d:
            call_command('agent_core_audit', '--synthetic', '--output', f'{d}/a.md', stdout=StringIO())
        self.assertEqual(order, ['collect', 'save'])
        run = AgentRun.objects.get()
        self.assertEqual((run.kind, run.status), ('audit', 'ok'))
        self.assertEqual(run.detail['counts']['duplicate_active_numbers'], 1)
        self.assertTrue(run.detail['report'].startswith('SYNTHETIC DATA'))
        self.assertTrue(run.detail['synthetic'])
