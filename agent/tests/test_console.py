"""Phase 3.2: Reconciliation Agent console (level 0)."""
import datetime

from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.utils import timezone

from agent import cycle
from agent.db import guard
from agent.models import AgentAction, AgentConfig, AgentRun, ScopeState
from agent.service import ensure_service_user

from . import factories as f
from .test_cycle import DAY, age_uploads, enable

VIEWS = ('/dashboard/agent/console/schedules/', '/dashboard/agent/console/reports/',
         '/dashboard/agent/console/theme-tester/')


class ConsoleBase(TestCase):
    def setUp(self):
        ensure_service_user()
        enable()
        self.acc, self.s = f.full_scope()
        age_uploads()
        cycle.run_cycle(now=DAY)                     # observed snapshots for the views
        self.admin = f.user(role='admin', email='boss@x.lk', accounts=[self.acc])
        self.client.force_login(self.admin)


class ReadOnlyViewsTest(ConsoleBase):
    def test_every_console_view_makes_no_core_writes(self):
        """READ ONLY transaction: on PostgreSQL any write raises CoreWriteAttempt."""
        urls = [f'{u}?month={self.s.month}' for u in VIEWS[:2]] + [
            f'{VIEWS[2]}?account_id={self.acc.id}&kind=tc&theme=NEXUS+30&duration=30',
            f'{VIEWS[2]}?account_id={self.acc.id}&kind=lmrb&theme=Nexus+(30)(Sin)&duration=30',
            '/dashboard/agent/']
        before = AgentAction.objects.count()
        for u in urls:
            with self.subTest(u):
                with guard(read_only=True):
                    r = self.client.get(u)
                self.assertEqual(r.status_code, 200)
        self.assertEqual(AgentAction.objects.count(), before)

    def test_schedules_and_reports_come_from_observed_snapshots(self):
        r = self.client.get(f'{VIEWS[0]}?month={self.s.month}')
        row = r.context['rows'][0]
        self.assertEqual((row['schedule_number'], row['planned']), ('101', 2))
        self.assertContains(r, f'/dashboard/summary/?account_id={self.acc.id}')
        r = self.client.get(f'{VIEWS[1]}?month={self.s.month}')
        self.assertEqual(r.context['rows'][0]['schedules'], 1)
        self.assertContains(r, '/dashboard/summary/pdf/')

    def test_no_approve_or_apply_controls(self):
        for u in VIEWS:
            body = self.client.get(f'{u}?month={self.s.month}').content.decode().lower()
            for word in ('approve', 'apply', 'reject'):
                self.assertNotIn(f'>{word}', body, (u, word))

    def test_theme_tester_resolves_with_engine_resolvers(self):
        r = self.client.get(f'{VIEWS[2]}?account_id={self.acc.id}&kind=tc&theme=nexus 30&duration=30')
        self.assertEqual([b['brand'] for b in r.context['result']['brands']], ['nexus'])
        r = self.client.get(f'{VIEWS[2]}?account_id={self.acc.id}&kind=lmrb&theme=Nexus (30)(Sin)&duration=30')
        self.assertEqual([b['brand'] for b in r.context['result']['brands']], ['nexus'])
        r = self.client.get(f'{VIEWS[2]}?account_id={self.acc.id}&kind=tc&theme=UNKNOWN&duration=30')
        self.assertEqual(r.context['result']['brands'], [])
        self.assertContains(r, 'No brand')

    def test_theme_tester_wildcard_lmrb(self):
        from core.models import BrandMapping
        BrandMapping.objects.create(account=self.acc, brand='Expo', theme='Ai Expo 2025*', tc_theme='AI EXPO')
        r = self.client.get(f'{VIEWS[2]}?account_id={self.acc.id}&kind=lmrb&theme=Ai Expo 2025_3 (30)(Sin)')
        self.assertEqual([b['brand'] for b in r.context['result']['brands']], ['expo'])

    def test_theme_tester_other_account_is_refused(self):
        other = f.account('Other Client')
        planner = f.user(role='planner', email='p@x.lk', accounts=[self.acc])
        self.client.force_login(planner)
        r = self.client.get(f'{VIEWS[2]}?account_id={other.id}&kind=tc&theme=X')
        self.assertEqual(r.status_code, 403)


class SwitchesTest(ConsoleBase):
    def test_run_cycle_only_sets_the_flag(self):
        cycles = AgentRun.objects.filter(kind='cycle').count()
        r = self.client.post('/dashboard/agent/console/run/')
        self.assertEqual(r.status_code, 302)
        cfg = AgentConfig.get_solo()
        self.assertIsNotNone(cfg.run_requested_at)
        self.assertEqual(AgentRun.objects.filter(kind='cycle').count(), cycles)        # nothing ran
        self.assertEqual(AgentRun.objects.filter(kind='scope').count(), 1)             # only the setUp cycle
        act = AgentAction.objects.get(action_type='agent_run_requested')
        self.assertEqual((act.actor_kind, act.human_confirmed, act.target_model), ('human', True, 'agent.AgentConfig'))
        self.assertIsNone(act.before['run_requested_at'])
        self.assertIsNotNone(act.after['run_requested_at'])

    def test_cycle_treats_request_as_run_now_and_clears_it(self):
        self.assertEqual(cycle.run_cycle(now=DAY)['scopes'], {})                        # fresh: nothing due
        self.client.post('/dashboard/agent/console/run/')
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['counts']['observed'], 1)                                  # run now
        self.assertIsNotNone(res['run_requested_at'])
        self.assertIsNone(AgentConfig.get_solo().run_requested_at)

    def test_newer_request_during_a_cycle_survives(self):
        cfg = AgentConfig.get_solo()
        cfg.run_requested_at = timezone.now() - datetime.timedelta(minutes=1)
        cfg.save()
        orig = cycle.select

        def select_and_click(*a, **k):
            AgentConfig.objects.filter(pk=1).update(run_requested_at=timezone.now())   # clicked mid-cycle
            return orig(*a, **k)
        from unittest import mock
        with mock.patch.object(cycle, 'select', side_effect=select_and_click):
            cycle.run_cycle(now=DAY)
        self.assertIsNotNone(AgentConfig.get_solo().run_requested_at)

    def test_pause_and_resume_are_logged_gate_writes(self):
        self.client.post('/dashboard/agent/console/pause/')
        self.assertFalse(AgentConfig.get_solo().enabled)
        self.client.post('/dashboard/agent/console/pause/')
        self.assertTrue(AgentConfig.get_solo().enabled)
        acts = list(AgentAction.objects.filter(action_type__in=('agent_pause', 'agent_resume')).order_by('id'))
        self.assertEqual([a.action_type for a in acts], ['agent_pause', 'agent_resume'])
        self.assertEqual((acts[0].before['enabled'], acts[0].after['enabled']), (True, False))
        self.assertTrue(all(a.actor_kind == 'human' and a.human_confirmed for a in acts))

    def test_switches_are_post_only(self):
        for u in ('/dashboard/agent/console/pause/', '/dashboard/agent/console/run/'):
            self.assertEqual(self.client.get(u).status_code, 405)
        self.assertTrue(AgentConfig.get_solo().enabled)
        self.assertIsNone(AgentConfig.get_solo().run_requested_at)
        self.assertFalse(AgentAction.objects.exists())

    def test_switches_are_admin_only(self):
        for role in ('team_head', 'planner', 'operations', 'channel_officer'):
            self.client.force_login(f.user(role=role, email=f'{role}@x.lk', accounts=[self.acc]))
            for u in ('/dashboard/agent/console/pause/', '/dashboard/agent/console/run/'):
                with self.subTest(role=role, url=u):
                    self.assertEqual(self.client.post(u).status_code, 403)
        cfg = AgentConfig.get_solo()
        self.assertTrue(cfg.enabled)
        self.assertIsNone(cfg.run_requested_at)
        self.assertFalse(AgentAction.objects.exists())

    def test_channel_officer_gets_403_everywhere(self):
        self.client.force_login(f.user(role='channel_officer', email='co@x.lk', accounts=[self.acc]))
        for u in VIEWS:
            self.assertEqual(self.client.get(u).status_code, 403)


class AgentCardTest(ConsoleBase):
    def render(self, user):
        req = RequestFactory().get('/dashboard/')
        req.user = user
        with guard(read_only=True):
            return Template('{% load agent_console %}{% agent_card %}').render(Context({'request': req}))

    def test_admin_sees_status_and_both_switches(self):
        html = self.render(self.admin)
        for text in ('Reconciliation Agent', 'Running · level 0', 'Last cycle', 'Next', 'Health',
                     '/dashboard/agent/console/pause/', '/dashboard/agent/console/run/'):
            self.assertIn(text, html)
        self.assertNotIn('Approve', html)

    def test_other_staff_see_status_without_switches(self):
        html = self.render(f.user(role='planner', email='p@x.lk'))
        self.assertIn('Reconciliation Agent', html)
        self.assertNotIn('/console/pause/', html)

    def test_channel_officer_sees_nothing(self):
        self.assertEqual(self.render(f.user(role='channel_officer', email='co@x.lk')).strip(), '')

    def test_paused_and_requested_states(self):
        cfg = AgentConfig.get_solo()
        cfg.enabled, cfg.run_requested_at = False, timezone.now()
        cfg.save()
        html = self.render(self.admin)
        self.assertIn('Paused', html)
        self.assertIn('Resume', html)
        self.assertIn('Run requested', html)

    def test_next_tick(self):
        from agent.console import next_tick
        t = timezone.make_aware(datetime.datetime(2026, 9, 26, 10, 7, 30))
        self.assertEqual(next_tick(t).strftime('%H:%M'), '10:15')
        self.assertEqual(next_tick(t.replace(minute=45, second=0)).strftime('%H:%M'), '11:00')


class BooleanFailSafeTest(TestCase):
    """Phase 3.2 item 2: every AgentConfig boolean becomes False when missing from the POST, and False
    is the safe value for each. A new boolean fails this test until its safe value is reviewed."""
    SAFE_WHEN_FALSE = {
        'enabled': 'kill switch: the agent does nothing but write its heartbeat',
        'intake_fetch_enabled': 'no mailbox is read',
        'intake_gemini_enabled': 'intake PDFs are not sent to Gemini; they are read once and go to review',
    }

    def test_boolean_fields_are_the_reviewed_ones(self):
        from django.db import models
        found = {fl.name for fl in AgentConfig._meta.get_fields() if isinstance(fl, models.BooleanField)}
        self.assertEqual(found, set(self.SAFE_WHEN_FALSE))

    def test_each_boolean_turns_off_when_missing_from_the_post(self):
        cfg = AgentConfig.get_solo()
        cfg.enabled = cfg.intake_fetch_enabled = cfg.intake_gemini_enabled = True
        cfg.save()
        admin = f.user(role='admin', email='boss@x.lk')
        self.client.force_login(admin)
        r = self.client.post('/dashboard/agent/config/', {
            'what': 'config', 'autonomy_level': 0, 'mapping_threshold': 0.92, 'grace_days': 3,
            'upload_debounce_minutes': 10, 'tc_intake_mode': 'off', 'min_brand_overlap': 0.6,
            'llm_daily_token_cap': 200000})
        self.assertEqual(r.status_code, 302)
        cfg.refresh_from_db()
        for name in self.SAFE_WHEN_FALSE:
            self.assertFalse(getattr(cfg, name), name)
