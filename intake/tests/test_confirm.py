"""Admin Confirm (C4), collision guard (A6), several TCs per schedule (C3), parity with
core tc_pdf_convert, and Q6 (human writes are not blocked by the kill switch)."""
import json
import os
import shutil
import tempfile
from datetime import date

from django.db import transaction
from django.test import Client, TestCase, override_settings

from agent import gate
from agent.models import AgentAction, AgentAuthorisation, AgentProposal, ScopeState, SummarySnapshot
from agent.tests import factories as f
from core.models import TCRow, TransmissionReport
from intake.confirm import ConfirmRefused, confirm_upload, eligible_schedules
from verification.tc_engine import reconcile_tc

from .helpers import attachment, enable, scenario, tc_rows

MEDIA = tempfile.mkdtemp(prefix='agent-media-')


@override_settings(MEDIA_ROOT=MEDIA)
class ConfirmTest(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.admin = f.user(role='admin')

    def test_off_mode_confirm_copies_exact_strings_and_never_reconciles(self):
        enable(mode='off', enabled=False)                  # agent OFF: a person may still confirm
        acc, s, att = scenario()
        s.channel = 'TV - Sirasa TV '                       # awkward exact string, trailing space
        s.save()
        att2 = attachment(rows=tc_rows(channel='TV - Sirasa TV'))
        res = confirm_upload(att2, s, self.admin)
        rep = TransmissionReport.objects.get(pk=res['tc_report_id'])
        self.assertEqual((rep.channel, rep.month, rep.schedule_id), ('TV - Sirasa TV ', 'January 2025', s.id))
        self.assertEqual(rep.channel.encode(), s.channel.encode())
        self.assertEqual(rep.uploaded_by_id, self.admin.id)
        self.assertEqual((rep.start_date, rep.end_date, rep.row_count), (date(2025, 1, 10), date(2025, 1, 13), 4))
        self.assertTrue(os.path.exists(os.path.join(MEDIA, rep.file.name)))
        self.assertFalse(TCRow.objects.filter(is_schedule_matched=True).exists())       # no reconcile
        self.assertTrue(ScopeState.objects.get(account=acc, channel=s.channel, month=s.month).needs_run)
        act = AgentAction.objects.get(action_type='intake_confirm_upload')
        self.assertEqual((act.actor_id, act.actor_kind, act.human_confirmed), (self.admin.id, 'human', True))
        att2.refresh_from_db()
        self.assertEqual((att2.status, att2.tc_report_id), ('uploaded', rep.id))

    def test_non_admin_cannot_confirm(self):
        acc, s, att = scenario()
        with self.assertRaises(gate.HumanNotAllowed):
            confirm_upload(att, s, f.user(role='operations', email='o@t.com'))
        self.assertFalse(TransmissionReport.objects.exists())

    def test_refusal_codes(self):
        acc, s, att = scenario()
        s.is_locked = True
        s.save()
        with self.assertRaisesMessage(ConfirmRefused, 'schedule_locked'):
            confirm_upload(att, s, self.admin)
        s.is_locked = False
        s.save()
        sc = ScopeState.objects.create(account=acc, channel=s.channel, month=s.month)
        snap = SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101', data={}, sha256='x')
        auth = AgentAuthorisation.objects.create(schedule=s, snapshot=snap, snapshot_sha256='x',
                                                 authorised_by=self.admin)
        with self.assertRaisesMessage(ConfirmRefused, 'schedule_frozen'):
            confirm_upload(att, s, self.admin)
        auth.delete()
        dup = f.schedule(acc, number='101', version=2)
        with self.assertRaisesMessage(ConfirmRefused, 'duplicate_active_number'):
            confirm_upload(att, dup, self.admin)
        self.assertFalse(TransmissionReport.objects.exists())

    def test_legacy_authorised_scope_is_frozen(self):
        from core.models import SummaryReportMeta
        acc, s, att = scenario()
        SummaryReportMeta.objects.create(account=acc, channel=s.channel, month=s.month, authorised_by='K. Perera')
        with self.assertRaisesMessage(ConfirmRefused, 'schedule_frozen'):
            confirm_upload(att, s, self.admin)
        self.assertNotIn(s.id, [x.id for x in eligible_schedules(att)])

    def test_dates_outside_window_need_the_second_tick(self):
        acc, s, att = scenario(rows=tc_rows(2, month=2, day0=20))       # Feb dates, Jan schedule
        with self.assertRaisesMessage(ConfirmRefused, 'date_out_of_range'):
            confirm_upload(att, s, self.admin)
        res = confirm_upload(att, s, self.admin, dates_ack=True)
        act = AgentAction.objects.get(action_type='intake_confirm_upload')
        self.assertTrue(act.evidence['dates_outside_window_ticked'])
        self.assertTrue(res['rows'])

    def test_dropdown_excludes_ineligible(self):
        acc, s, att = scenario()
        locked = f.schedule(acc, number='201', locked=True)
        f.schedule(acc, number='301')
        f.schedule(acc, number='301', version=2)                     # duplicate active number
        ids = [x.id for x in eligible_schedules(att)]
        self.assertIn(s.id, ids)
        self.assertNotIn(locked.id, ids)
        self.assertFalse([x for x in eligible_schedules(att) if x.schedule_number == '301'])

    def test_collision_rolls_back_and_proposes(self):
        acc, s, att = scenario()
        other = f.tc_report(acc, None)                               # standalone report, same channel
        kept = f.tc_row(acc, other, day=10, time='20:00:00', theme='NEXUS 30', dur=30)
        media_before = sum(len(fs) for _, _, fs in os.walk(MEDIA))
        with self.assertRaisesMessage(ConfirmRefused, 'conflict'):
            confirm_upload(att, s, self.admin)
        self.assertTrue(TCRow.objects.filter(pk=kept.pk).exists())
        self.assertEqual(TransmissionReport.objects.count(), 1)
        self.assertEqual(sum(len(fs) for _, _, fs in os.walk(MEDIA)), media_before)   # no file written
        p = AgentProposal.objects.get(action_type='intake_collision')
        self.assertEqual((p.kind, p.schedule_id, p.evidence['replaced_tc_row_ids']), ('tc_link', s.id, [kept.id]))
        att.refresh_from_db()
        self.assertEqual((att.status, att.reason), ('needs_review', 'conflict'))
        self.assertFalse(AgentAction.objects.filter(action_type='intake_confirm_upload').exists())

    def test_collision_with_same_schedule_is_tc_already_exists(self):
        acc, s, att = scenario()
        first = f.tc_report(acc, s)
        f.tc_row(acc, first, day=10, time='20:00:00', theme='NEXUS 30', dur=30)
        with self.assertRaisesMessage(ConfirmRefused, 'tc_already_exists'):
            confirm_upload(att, s, self.admin)

    def test_second_tc_for_same_schedule_is_counted(self):
        """C3: reconcile_tc(schedule_id) takes rows of EVERY report linked to the schedule."""
        acc, s, att = scenario(rows=tc_rows(1, day0=10))
        f.row(acc, s, brand='Nexus', day=20)
        f.lmrb(acc, day=10, time='20:00:00', theme='Nexus (30)(Sin)')
        f.lmrb(acc, day=20, time='20:00:00', theme='Nexus (30)(Sin)')
        confirm_upload(att, s, self.admin)
        second = attachment(rows=tc_rows(1, day0=20))
        confirm_upload(second, s, self.admin)
        self.assertEqual(TransmissionReport.objects.filter(schedule=s).count(), 2)
        with transaction.atomic():
            res = reconcile_tc(acc.id, s.channel, s.month, mode='smart', schedule_id=s.id)
            matched = TCRow.objects.filter(tc_report__schedule=s, is_schedule_matched=True).count()
            transaction.set_rollback(True)
        self.assertEqual(matched, 2, res)


@override_settings(MEDIA_ROOT=MEDIA)
class ParityWithTcPdfConvertTest(TestCase):
    """Parity baseline (A6): the rows core tc_pdf_convert saves vs the rows Confirm saves,
    for the same parsed rows. The known differences are asserted, not hidden."""

    def test_same_rows_recorded_differences(self):
        admin = f.user(role='admin')
        rows = tc_rows(3)
        # core path, account A
        a = f.account('ParityA')
        c = Client()
        c.force_login(admin)
        payload = {'account_id': str(a.id), 'channel': 'Sirasa TV', 'rows': [
            {'Date': f'{int(r[1][8:10])}/{int(r[1][5:7])}/{r[1][:4]}', 'Programme': r[2], 'TC Theme': r[3],
             'Duration': r[4], 'Aired Time': r[5]} for r in rows]}
        resp = c.post('/dashboard/tc/pdf-convert/', data=json.dumps(payload), content_type='application/json')
        self.assertTrue(resp.json()['ok'], resp.content)
        core_rep = TransmissionReport.objects.get(account=a)
        core_rows = set(TCRow.objects.filter(tc_report=core_rep)
                        .values_list('date', 'aired_time', 'tc_theme', 'duration', 'programme'))
        # intake path, account B (the default scenario account)
        acc, s, att = scenario(rows=rows)
        res = confirm_upload(att, s, admin)
        intake_rep = TransmissionReport.objects.get(pk=res['tc_report_id'])
        intake_rows = set(TCRow.objects.filter(tc_report=intake_rep)
                          .values_list('date', 'aired_time', 'tc_theme', 'duration', 'programme'))
        self.assertEqual(core_rows, intake_rows)
        # recorded differences
        self.assertIsNone(core_rep.schedule_id)                    # core: no schedule link
        self.assertEqual(intake_rep.schedule_id, s.id)
        self.assertEqual(core_rep.file.name, '')                   # core: no file stored
        self.assertTrue(intake_rep.file.name)
        self.assertEqual(core_rep.month, 'January 2025')           # core: month from TC dates
        self.assertEqual(intake_rep.month, s.month)                # intake: month from the Schedule
