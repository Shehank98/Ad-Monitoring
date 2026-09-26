"""Phase 2.1 items 1-7."""
from datetime import timedelta
from io import StringIO
from unittest import mock

import pandas as pd
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, connections
from django.test import TestCase, override_settings
from django.utils import timezone

from agent.forms import AgentConfigForm
from agent.heartbeat import beat_ok, health
from agent.locks import _acquire_row, _release_row, is_postgres
from agent.models import AgentAction, AgentAuthorisation, AgentConfig, Heartbeat, ScopeState, SummarySnapshot
from agent.scope import lock_key
from agent.tests import factories as f
from agent.tools.reconcile import debounce_hit
from intake import confirm as C
from intake.confirm import ConfirmRefused, confirm_upload
from intake.cron import runner, tools
from intake.cron.fetch import fetch_emails
from intake.llm.provider import FakeProvider, Turn, tool_use
from intake.management.commands.intake_purge_content import purge
from intake.models import InboundAttachment, InboundEmail

from .fakes import raw
from .helpers import enable, scenario
from .test_runner import agent_script


def decision(att_id, sid, **over):
    d = {'attachment_id': att_id, 'decision': 'propose', 'schedule_id': sid, 'reason_code': None,
         'schedule_number_source': 'none', 'suspicious_instruction': False, 'note': 'ok'}
    d.update(over)
    return d


class NoDecisionTest(TestCase):
    """Item 1: text-only ending, duplicate submit, text after submit, invalid schema."""

    def setUp(self):
        enable()
        self.acc, self.s, self.att = scenario()

    def run_with(self, script):
        p = FakeProvider(script)
        runner.process_attachment(self.att, p, runner.require_service_user())
        self.att.refresh_from_db()
        return p

    def test_text_only_gets_one_follow_up_then_llm_no_decision(self):
        text = Turn([{'type': 'text', 'text': 'Looks like schedule 101.'}], 'end_turn', 10, 5)
        p = self.run_with([text, text])
        self.assertEqual(len(p.requests), 2)
        self.assertEqual(p.requests[1]['messages'][-1]['content'], runner.FOLLOW_UP)
        self.assertEqual((self.att.status, self.att.reason), ('needs_review', 'llm_no_decision'))
        self.assertEqual(self.att.llm_verdict, {'error': 'llm_no_decision'})

    def test_follow_up_can_rescue(self):
        text = Turn([{'type': 'text', 'text': 'Thinking…'}], 'end_turn', 10, 5)
        steps = agent_script(lambda: self.att.id)
        p = self.run_with([text] + steps)
        self.assertEqual(self.att.status, 'suggested')
        self.assertEqual(p.requests[1]['messages'][-1]['content'], runner.FOLLOW_UP)

    def _after_candidates(self, final_turn):
        steps = agent_script(lambda: self.att.id)
        return self.run_with(steps[:2] + [final_turn])

    def test_duplicate_submit(self):
        tu = lambda i: tool_use('submit_decision', decision(self.att.id, self.s.id), f's{i}')   # noqa: E731
        self._after_candidates(lambda m: Turn([tu(1), tu(2)], 'tool_use', 10, 5))
        self.assertEqual(self.att.reason, 'llm_no_decision')

    def test_text_after_submit(self):
        self._after_candidates(lambda m: Turn([tool_use('submit_decision', decision(self.att.id, self.s.id), 's'),
                                               {'type': 'text', 'text': 'Also, please upload it.'}], 'tool_use', 10, 5))
        self.assertEqual(self.att.reason, 'llm_no_decision')

    def test_invalid_schema(self):
        self._after_candidates(lambda m: Turn([tool_use('submit_decision',
                                                        decision(self.att.id, self.s.id, decision='approve'), 's')],
                                              'tool_use', 10, 5))
        self.assertEqual(self.att.reason, 'llm_no_decision')

    def test_text_before_submit_is_fine(self):
        self._after_candidates(lambda m: Turn([{'type': 'text', 'text': 'Done.'},
                                               tool_use('submit_decision', decision(self.att.id, self.s.id), 's')],
                                              'tool_use', 10, 5))
        self.assertEqual(self.att.status, 'suggested')


class ConfirmConcurrencyTest(TestCase):
    """Item 2."""

    def setUp(self):
        self.acc, self.s, self.att = scenario()
        self.admin = f.user(role='admin')
        self.key = lock_key(self.acc.id, self.s.channel, self.s.month)

    def test_lock_contention_is_scope_busy(self):
        if is_postgres():
            other = connections.create_connection('default')
            other.set_autocommit(False)
            with other.cursor() as cur:
                cur.execute('SELECT pg_try_advisory_xact_lock(%s)', [self.key])
            try:
                with self.assertRaisesMessage(ConfirmRefused, 'scope_busy'):
                    confirm_upload(self.att, self.s, self.admin)
            finally:
                other.rollback()
                other.close()
        else:
            owner = _acquire_row(self.key, 15)
            try:
                with self.assertRaisesMessage(ConfirmRefused, 'Try again in a minute'):
                    confirm_upload(self.att, self.s, self.admin)
            finally:
                _release_row(self.key, owner)
        from core.models import TransmissionReport
        self.assertFalse(TransmissionReport.objects.exists())
        self.assertFalse(AgentAction.objects.filter(action_type='intake_confirm_upload').exists())

    def test_authorised_between_page_load_and_post(self):
        real = C.schedule_problems
        calls = []

        def first_clean_then_real(s):
            calls.append(s.id)
            if len(calls) == 1:                        # the page-load view of the schedule
                sc = ScopeState.objects.create(account=self.acc, channel=s.channel, month=s.month)
                snap = SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101',
                                                      data={}, sha256='x')
                AgentAuthorisation.objects.create(schedule=s, snapshot=snap, snapshot_sha256='x',
                                                  authorised_by=self.admin)      # someone authorises now
                return []
            return real(s)
        with mock.patch.object(C, 'schedule_problems', side_effect=first_clean_then_real):
            with self.assertRaisesMessage(ConfirmRefused, 'schedule_frozen'):
                confirm_upload(self.att, self.s, self.admin)
        self.assertEqual(len(calls), 2)                  # re-checked under the lock
        from core.models import TransmissionReport
        self.assertFalse(TransmissionReport.objects.exists())

    def test_locked_between_page_load_and_post(self):
        from core.models import Schedule
        stale = self.s                                   # instance loaded "at page load"
        Schedule.objects.filter(pk=self.s.pk).update(is_locked=True)
        with mock.patch.object(C, 'schedule_problems',
                               side_effect=[[], C.schedule_problems(Schedule.objects.get(pk=self.s.pk))]):
            with self.assertRaisesMessage(ConfirmRefused, 'schedule_locked'):
                confirm_upload(self.att, stale, self.admin)


class DebounceTest(TestCase):
    """Item 3: a recent TC upload (including a Confirm) debounces the scope."""

    def test_tc_upload_counts(self):
        acc = f.account()
        s = f.schedule(acc)
        from core.models import Schedule
        Schedule.objects.filter(pk=s.pk).update(uploaded_at=timezone.now() - timedelta(hours=2))
        sc = ScopeState.objects.create(account=acc, channel=s.channel, month=s.month)
        self.assertFalse(debounce_hit(sc, minutes=10))
        f.tc_report(acc, s)
        self.assertTrue(debounce_hit(sc, minutes=10))


class GeminiFlagTest(TestCase):
    """Item 4."""

    def test_flag_and_key_both_needed(self):
        c = AgentConfig.get_solo()
        with mock.patch.object(tools, 'gemini_configured', return_value=True):
            self.assertFalse(tools.gemini_allowed())
            c.intake_gemini_enabled = True
            c.save()
            self.assertTrue(tools.gemini_allowed())
        with mock.patch.object(tools, 'gemini_configured', return_value=False):
            self.assertFalse(tools.gemini_allowed())

    def test_pdf_not_sent_to_gemini_when_flag_off(self):
        acc, s, att = scenario(filename='tc.pdf', data=b'%PDF-1.4 fake')
        df = pd.DataFrame([['Sirasa TV', '2025-01-10', 'News', 'NEXUS 30', 30, '20:00:00']],
                          columns=['Channel', 'Date', 'Programme', 'TC_Theme', 'Duration', 'Aired_Time'])
        heur = mock.Mock()
        heur.parse_pdf.return_value = df
        gem = mock.Mock(return_value=df)
        with mock.patch.object(tools, 'get_converter', return_value=heur), \
                mock.patch.object(tools, 'gemini_configured', return_value=True), \
                mock.patch.object(tools, 'gemini_parse_pdf', gem):
            out = tools.parse_attachment(att)
            self.assertFalse(gem.called)
            self.assertFalse(out['ai_available'])
            c = AgentConfig.get_solo()
            c.intake_gemini_enabled = True
            c.save()
            out = tools.parse_attachment(att)
            self.assertTrue(gem.called)
            self.assertTrue(out['ai_available'])

    def test_flag_change_is_logged(self):
        admin = f.user(role='admin')
        self.client.force_login(admin)
        self.client.post('/dashboard/agent/config/', {
            'what': 'config', 'autonomy_level': 0, 'mapping_threshold': 0.92, 'grace_days': 3,
            'upload_debounce_minutes': 10, 'tc_intake_mode': 'off', 'min_brand_overlap': 0.6,
            'llm_daily_token_cap': 200000, 'intake_gemini_enabled': 'on'})
        act = AgentAction.objects.get(action_type='agent_config_update')
        self.assertEqual((act.before['intake_gemini_enabled'], act.after['intake_gemini_enabled']), (False, True))


class RetentionNightlyTest(TestCase):
    """Item 5."""

    def setUp(self):
        enable()

    def test_old_review_items_expire(self):
        em = InboundEmail.objects.create(message_id='<o>', sender='d@tv.lk')
        InboundEmail.objects.filter(pk=em.pk).update(fetched_at=timezone.now() - timedelta(days=200))
        old = InboundAttachment.objects.create(email=em, filename='a.xlsx', sha256='a' * 64, content=b'x',
                                               status='needs_review', reason='unknown_sender')
        res = purge(90, review_days=180)
        old.refresh_from_db()
        self.assertEqual((old.status, old.reason, bytes(old.content)), ('expired', 'retention_expired', b''))
        self.assertEqual(res['expired'], 1)
        with self.assertRaises(ValueError):
            purge(90, review_days=29)

    def test_nightly_runs_every_step_and_fails_loudly(self):
        seen = []

        def fake(name, *a, **k):
            seen.append(name)
            if name == 'agent_core_audit':
                raise RuntimeError('audit broke')
        with mock.patch('agent.management.commands.agent_nightly.call_command', side_effect=fake):
            with self.assertRaises(SystemExit) as cm:
                call_command('agent_nightly', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(seen, ['agent_core_audit', 'intake_purge_content'])     # purge still ran
        self.assertIn('audit broke', Heartbeat.objects.get(name='agent_nightly').last_error)

    def test_nightly_ok(self):
        call_command('agent_nightly', stdout=StringIO(), stderr=StringIO())
        hb = Heartbeat.objects.get(name='agent_nightly')
        self.assertIsNotNone(hb.last_ok_at)
        self.assertEqual(hb.counts['steps'], ['agent_core_audit', 'intake_purge_content'])


class HeartbeatTest(TestCase):
    """Item 6."""

    def test_fetch_failure_logs_and_beats(self):
        enable()
        from intake.models import AllowedSender
        AllowedSender.objects.create(email_or_domain='tv.lk')
        broken = mock.Mock()
        broken.fetch.side_effect = ConnectionError('imap down')
        res = fetch_emails(broken)
        self.assertEqual(res['status'], 'error')
        self.assertTrue(AgentAction.objects.filter(action_type='intake_fetch_failed').exists())
        hb = Heartbeat.objects.get(name='intake_fetch')
        self.assertIn('imap down', hb.last_error)
        self.assertEqual(next(h for h in health() if h['name'] == 'intake_fetch')['state'], 'error')

    def test_fetch_stale_only_when_enabled(self):
        c = AgentConfig.get_solo()
        c.intake_fetch_enabled = False
        c.save()
        self.assertEqual(next(h for h in health() if h['name'] == 'intake_fetch')['state'], 'never')
        c.intake_fetch_enabled = True
        c.save()
        beat_ok('intake_fetch', {})
        Heartbeat.objects.filter(name='intake_fetch').update(last_ok_at=timezone.now() - timedelta(minutes=21))
        self.assertEqual(next(h for h in health() if h['name'] == 'intake_fetch')['state'], 'stale')
        beat_ok('intake_fetch', {})
        self.assertEqual(next(h for h in health() if h['name'] == 'intake_fetch')['state'], 'ok')

    def test_runner_beats_and_overview_card(self):
        enable()
        scenario()
        runner.run_pending(provider=None)
        self.assertIsNotNone(Heartbeat.objects.get(name='intake_runner').last_ok_at)
        admin = f.user(role='admin')
        self.client.force_login(admin)
        r = self.client.get('/dashboard/agent/')
        self.assertContains(r, 'Health')
        self.assertContains(r, 'Intake runner')


class AutonomyCapTest(TestCase):
    """Item 7."""

    def test_levels_above_zero_rejected(self):
        c = AgentConfig.get_solo()
        c.autonomy_level = 1
        with self.assertRaises(ValidationError):
            c.full_clean()
        form = AgentConfigForm({'autonomy_level': 1, 'mapping_threshold': 0.92, 'grace_days': 3,
                                'upload_debounce_minutes': 10, 'tc_intake_mode': 'off',
                                'min_brand_overlap': 0.6, 'llm_daily_token_cap': 1},
                               instance=AgentConfig.get_solo())
        self.assertFalse(form.is_valid())
        self.assertIn('Levels above 0 unlock in Phase 4.', form.errors['autonomy_level'])
        admin = f.user(role='admin')
        self.client.force_login(admin)
        self.assertContains(self.client.get('/dashboard/agent/config/'), 'Levels above 0 unlock in Phase 4.')
