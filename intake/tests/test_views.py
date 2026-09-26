"""UI access and admin actions (Phase 2 A, B, Q5, Q6)."""
import shutil
import tempfile

from django.test import TestCase, override_settings

from agent.models import AgentAction, AgentAccountOverride, AgentConfig, AgentRun, ScopeState
from agent.tests import factories as f
from core.models import TransmissionReport
from intake.models import AllowedSender

from .helpers import enable, scenario

MEDIA = tempfile.mkdtemp(prefix='agent-media-ui-')


@override_settings(MEDIA_ROOT=MEDIA)
class InboxViewsTest(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.acc, self.s, self.att = scenario()
        self.admin = f.user(role='admin')

    def login(self, user):
        self.client.force_login(user)

    def test_access_by_role(self):
        self.login(self.admin)
        for url in ('/dashboard/agent/inbox/', f'/dashboard/agent/inbox/{self.att.id}/', '/dashboard/agent/config/',
                    '/dashboard/agent/', '/dashboard/agent/activity/'):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        officer = f.user(role='channel_officer', email='co@t.com')
        self.login(officer)
        self.assertEqual(self.client.get('/dashboard/agent/inbox/').status_code, 403)
        planner = f.user(role='planner', email='p@t.com', accounts=[self.acc])
        self.login(planner)
        self.assertEqual(self.client.get('/dashboard/agent/inbox/').status_code, 200)
        self.assertEqual(self.client.get('/dashboard/agent/config/').status_code, 403)

    def test_list_shows_every_item_and_filters(self):
        from .helpers import attachment
        b = attachment(filename='b.xlsx')
        b.status, b.reason = 'needs_review', 'low_brand_overlap'
        b.save()
        self.login(self.admin)
        r = self.client.get('/dashboard/agent/inbox/')
        self.assertContains(r, self.att.filename)
        self.assertContains(r, 'b.xlsx')                       # regression: '' reason filter hid it
        r = self.client.get('/dashboard/agent/inbox/?reason=low_brand_overlap')
        self.assertNotContains(r, self.att.filename)
        self.assertContains(r, 'b.xlsx')

    def test_users_see_only_their_clients(self):
        other = f.account('Dialog')
        planner = f.user(role='planner', email='p@t.com', accounts=[other])
        self.att.suggested_schedule = self.s
        self.att.save()
        self.login(planner)
        self.assertNotContains(self.client.get('/dashboard/agent/inbox/'), self.att.filename)
        self.assertEqual(self.client.get(f'/dashboard/agent/inbox/{self.att.id}/').status_code, 404)
        mine = f.user(role='operations', email='o@t.com', accounts=[self.acc])
        self.login(mine)
        self.assertContains(self.client.get('/dashboard/agent/inbox/'), self.att.filename)
        self.assertNotContains(self.client.get(f'/dashboard/agent/inbox/{self.att.id}/'), 'Confirm and upload')

    def test_confirm_is_admin_and_post_only(self):
        url = f'/dashboard/agent/inbox/{self.att.id}/decide/'
        planner = f.user(role='planner', email='p@t.com', accounts=[self.acc])
        self.login(planner)
        self.assertEqual(self.client.post(url, {'action': 'confirm', 'schedule_id': self.s.id}).status_code, 403)
        self.login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertFalse(TransmissionReport.objects.exists())
        r = self.client.post(url, {'action': 'confirm', 'schedule_id': self.s.id}, follow=True)
        self.assertContains(r, 'Uploaded 4 TC rows')
        rep = TransmissionReport.objects.get()
        self.assertEqual((rep.channel, rep.month, rep.schedule_id), (self.s.channel, self.s.month, self.s.id))

    def test_confirm_refuses_ineligible_and_shows_reason(self):
        self.login(self.admin)
        locked = f.schedule(self.acc, number='777', locked=True)
        url = f'/dashboard/agent/inbox/{self.att.id}/decide/'
        r = self.client.post(url, {'action': 'confirm', 'schedule_id': locked.id}, follow=True)
        self.assertContains(r, 'Choose one of the listed schedules')
        self.assertNotContains(self.client.get(f'/dashboard/agent/inbox/{self.att.id}/'), '#777')
        self.assertFalse(TransmissionReport.objects.exists())

    def test_processed_detail_renders_both_verdicts(self):
        from intake.cron import runner
        enable()
        runner.process_attachment(self.att, None, runner.require_service_user())
        self.login(self.admin)
        r = self.client.get(f'/dashboard/agent/inbox/{self.att.id}/')
        self.assertContains(r, 'Candidate schedules')
        self.assertContains(r, 'all pass')
        self.assertContains(r, 'no model configured')
        self.assertContains(r, 'Confirm and upload')

    def test_ignore_and_reject_logged_as_human(self):
        self.login(self.admin)
        self.client.post(f'/dashboard/agent/inbox/{self.att.id}/decide/', {'action': 'ignore', 'note': 'rate card'})
        self.att.refresh_from_db()
        self.assertEqual(self.att.status, 'ignored')
        self.assertTrue(AgentAction.objects.get(action_type='intake_ignored').human_confirmed)


class SettingsViewTest(TestCase):
    def setUp(self):
        self.admin = f.user(role='admin')
        self.client.force_login(self.admin)

    def post_config(self, **over):
        data = {'what': 'config', 'autonomy_level': 0, 'mapping_threshold': 0.92, 'grace_days': 3,
                'upload_debounce_minutes': 10, 'tc_intake_mode': 'off', 'min_brand_overlap': 0.6,
                'llm_daily_token_cap': 200000, **f.PHASE3_CONFIG_POST}
        data.update(over)
        return self.client.post('/dashboard/agent/config/', data, follow=True)

    def test_auto_mode_rejected(self):
        r = self.post_config(tc_intake_mode='auto')
        self.assertContains(r, 'Select a valid choice')
        self.assertEqual(AgentConfig.get_solo().tc_intake_mode, 'off')
        self.assertFalse(AgentAction.objects.exists())

    def test_save_is_logged_even_with_agent_disabled(self):
        r = self.post_config(tc_intake_mode='suggest', intake_fetch_enabled='on', min_brand_overlap=0.7)
        self.assertContains(r, 'Agent settings saved')
        c = AgentConfig.get_solo()
        self.assertEqual((c.enabled, c.tc_intake_mode, c.intake_fetch_enabled, c.min_brand_overlap),
                         (False, 'suggest', True, 0.7))
        act = AgentAction.objects.get(action_type='agent_config_update')
        self.assertEqual((act.human_confirmed, act.before['tc_intake_mode'], act.after['tc_intake_mode']),
                         (True, 'off', 'suggest'))

    def test_senders_and_overrides(self):
        acc = f.account()
        self.client.post('/dashboard/agent/config/', {'what': 'sender_add', 'email_or_domain': 'Desk@TV.lk',
                                                       'channel_hint': 'Sirasa TV', 'accounts': [acc.id]})
        s = AllowedSender.objects.get()
        self.assertEqual(s.email_or_domain, 'desk@tv.lk')
        self.client.post('/dashboard/agent/config/', {'what': 'sender_toggle', 'id': s.id})
        s.refresh_from_db()
        self.assertFalse(s.active)
        bad = self.client.post('/dashboard/agent/config/', {'what': 'sender_add', 'email_or_domain': 'not an address'})
        self.assertContains(bad, 'Enter an exact address')
        self.client.post('/dashboard/agent/config/', {'what': 'override', 'account': acc.id, 'enabled': '0',
                                                       'autonomy_level': ''})
        self.assertIs(AgentAccountOverride.objects.get(account=acc).enabled, False)
        self.client.post('/dashboard/agent/config/', {'what': 'sender_delete', 'id': s.id})
        self.assertFalse(AllowedSender.objects.exists())
        kinds = list(AgentAction.objects.order_by('id').values_list('action_type', flat=True))
        self.assertEqual(kinds, ['allowed_sender_add', 'allowed_sender_toggle', 'agent_override_update',
                                 'allowed_sender_delete'])
        self.assertTrue(all(AgentAction.objects.values_list('human_confirmed', flat=True)))


class OverviewAuditCardTest(TestCase):
    def test_audit_card_and_report(self):
        admin = f.user(role='admin')
        run = AgentRun.objects.create(kind='audit', status='ok', detail={
            'synthetic': True, 'counts': {'duplicate_active_numbers': 2, 'lock_orphaned': 0},
            'report': 'SYNTHETIC DATA\n# Core audit'})
        self.client.force_login(admin)
        r = self.client.get('/dashboard/agent/')
        self.assertContains(r, 'Duplicate active numbers')
        self.assertContains(r, f'/dashboard/agent/audit/{run.id}/')
        self.assertContains(self.client.get(f'/dashboard/agent/audit/{run.id}/'), '# Core audit')
        planner = f.user(role='planner', email='p@t.com')
        self.client.force_login(planner)
        self.assertEqual(self.client.get(f'/dashboard/agent/audit/{run.id}/').status_code, 403)


class ActivityScopeTest(TestCase):
    def test_users_see_their_clients_only(self):
        a, b = f.account('A'), f.account('B')
        sa = ScopeState.objects.create(account=a, channel='X', month='January 2025')
        sb = ScopeState.objects.create(account=b, channel='X', month='January 2025')
        AgentAction.objects.create(action_type='mine_scope', scope=sa, reason='for A')
        AgentAction.objects.create(action_type='other_scope', scope=sb, reason='for B')
        u = f.user(role='team_head', email='t@t.com', accounts=[a])
        self.client.force_login(u)
        r = self.client.get('/dashboard/agent/activity/')
        self.assertContains(r, 'mine_scope')
        self.assertNotContains(r, 'other_scope')
