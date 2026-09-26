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
        r = self.client.get(f'{VIEWS[2]}?account_id={self.acc.id}&kind=lmrb&theme=Ai Expo 2025_3 (30)(Sin)&duration=30')
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



class GuardianFindingsTest(ConsoleBase):
    """Phase 3.2 guardian findings on the console."""

    def test_superseded_version_is_not_counted_twice(self):
        from core.models import Schedule
        v2 = f.schedule(self.acc, number='101', version=2)
        for d in (10, 12, 14):
            f.row(self.acc, v2, day=d)
        Schedule.objects.filter(pk=self.s.pk).update(is_superseded=True)
        age_uploads()
        from agent.models import AgentRun
        AgentRun.objects.filter(kind='scope').update(started_at=timezone.now() - datetime.timedelta(hours=7))
        cycle.run_cycle(now=DAY)
        r = self.client.get(f'{VIEWS[0]}?month={self.s.month}')
        self.assertEqual([(x['schedule_id'], x['planned']) for x in r.context['rows']], [(v2.id, 3)])
        r = self.client.get(f'{VIEWS[1]}?month={self.s.month}')
        self.assertEqual((r.context['rows'][0]['schedules'], r.context['rows'][0]['planned']), (1, 3))

    def test_signoff_comes_from_scope_state(self):
        from core.models import SummaryReportMeta
        SummaryReportMeta.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month,
                                         authorised_by='Finance Head')
        from agent.models import AgentRun
        AgentRun.objects.filter(kind='scope').update(started_at=timezone.now() - datetime.timedelta(hours=7))
        cycle.run_cycle(now=DAY)
        r = self.client.get(f'{VIEWS[1]}?month={self.s.month}')
        self.assertEqual(r.context['rows'][0]['signoff'], 'Authorised by Finance Head')

    def test_theme_tester_bad_input_never_500(self):
        for q in ('account_id=abc&kind=tc&theme=X&duration=30', f'account_id={self.acc.id}&kind=tc&theme=X',
                  f'account_id={self.acc.id}&kind=tc&theme=X&duration=3o', f'account_id={self.acc.id}&kind=zz&theme=X&duration=30'):
            with self.subTest(q):
                r = self.client.get(f'{VIEWS[2]}?{q}')
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.context['error'])
                self.assertIsNone(r.context['result'])

    def test_pause_message_only_on_success(self):
        from unittest import mock
        from agent import gate
        with mock.patch.object(gate, 'perform', side_effect=gate.HumanNotAllowed('no')):
            r = self.client.post('/dashboard/agent/console/pause/', follow=True)
        texts = [m.message for m in r.context['messages']]
        self.assertIn('no', texts)
        self.assertFalse(any('paused' in t for t in texts))


class RunRequestSemanticsTest(ConsoleBase):
    """Owner item 6: what run_requested_at changes in the next cycle."""

    def request_run(self):
        return self.client.post('/dashboard/agent/console/run/', follow=True)

    def test_every_scope_counts_as_due(self):
        self.assertEqual(cycle.run_cycle(now=DAY)['scopes'], {})           # fresh: nothing due
        self.request_run()
        res = cycle.run_cycle(now=DAY)
        self.assertEqual(res['scopes'], {ScopeState.objects.get().id: 'ok'})
        self.assertEqual(AgentRun.objects.filter(kind='scope').order_by('-id').first().detail['why'], 'due')

    def test_same_cap(self):
        f.full_scope(self.acc, number='201', channel='Derana TV')
        age_uploads()
        enable(max_scopes_per_cycle=1)
        self.request_run()
        res = cycle.run_cycle(now=DAY)
        self.assertEqual((res['counts']['observed'], res['capped']), (1, 1))

    def test_same_debounce(self):
        from core.models import Schedule
        self.request_run()
        Schedule.objects.update(uploaded_at=timezone.now())
        res = cycle.run_cycle(now=DAY)
        self.assertEqual((res['counts']['debounced'], res['counts']['observed']), (1, 0))

    def test_observe_only_outside_the_shadow_window(self):
        self.request_run()
        res = cycle.run_cycle(now=DAY)
        self.assertFalse(res['in_window'])
        self.assertFalse(AgentRun.objects.filter(kind__in=('dry_run', 'shadow_window')).exists())
        self.assertNotIn('shadow', AgentRun.objects.filter(kind='scope').order_by('-id').first().detail)

    def test_second_click_while_pending_changes_nothing(self):
        self.request_run()
        first = AgentConfig.get_solo().run_requested_at
        r = self.request_run()
        self.assertEqual(AgentConfig.get_solo().run_requested_at, first)
        self.assertEqual(AgentAction.objects.filter(action_type='agent_run_requested').count(), 1)
        self.assertTrue(any('already requested' in m.message for m in r.context['messages']))

    def test_refused_while_paused(self):
        enable(enabled=False)
        r = self.request_run()
        self.assertIsNone(AgentConfig.get_solo().run_requested_at)
        self.assertFalse(AgentAction.objects.exists())
        self.assertTrue(any('paused' in m.message for m in r.context['messages']))


class CardSafetyTest(ConsoleBase):
    """Owner item 5: the card never breaks a page, runs a fixed number of queries, hides itself."""

    def render(self, user):
        req = RequestFactory().get('/dashboard/')
        req.user = user
        return Template('{% load agent_console %}{% agent_card %}').render(Context({'request': req}))

    def test_any_exception_renders_nothing_and_logs(self):
        from unittest import mock
        with mock.patch('agent.console._card', side_effect=RuntimeError('boom')):
            with self.assertLogs('agent.console', level='ERROR') as logs:
                self.assertEqual(self.render(self.admin).strip(), '')
        self.assertIn('agent card failed', logs.output[0])

    def test_agent_tables_missing_renders_nothing(self):
        from unittest import mock
        from django.db import ProgrammingError
        with mock.patch('agent.console.AgentConfig.objects.filter',
                        side_effect=ProgrammingError('relation "agent_agentconfig" does not exist')):
            with self.assertLogs('agent.console', level='ERROR'):
                self.assertEqual(self.render(self.admin).strip(), '')

    def test_fixed_small_number_of_queries(self):
        with self.assertNumQueries(4):     # AgentConfig, last cycle run, health(): AgentConfig + heartbeats
            self.render(self.admin)
        f.full_scope(f.account('Second'), number='301', channel='Hiru TV')
        age_uploads()
        cycle.run_cycle(now=DAY)
        with self.assertNumQueries(4):     # same count with more scopes, runs and heartbeats
            self.render(self.admin)

    def test_hidden_for_channel_officer_and_anonymous_without_queries(self):
        from django.contrib.auth.models import AnonymousUser
        for user in (f.user(role='channel_officer', email='co@x.lk'), AnonymousUser()):
            with self.subTest(user=str(user)):
                with self.assertNumQueries(0):
                    self.assertEqual(self.render(user).strip(), '')

    def test_no_request_in_context_renders_nothing(self):
        self.assertEqual(Template('{% load agent_console %}{% agent_card %}').render(Context({})).strip(), '')


class Patch0007Test(TestCase):
    def test_applies_after_0001_and_0005(self):
        import pathlib
        import shutil
        import subprocess
        import tempfile
        from unittest import SkipTest
        if shutil.which('git') is None:
            raise SkipTest('git not available')
        root = pathlib.Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'templates').mkdir()
            shutil.copy(root / 'templates' / 'base.html', pathlib.Path(tmp) / 'templates' / 'base.html')
            patches = root / 'docs' / 'agent' / 'patches'
            for name in ('0001_base_nav_rename.diff', '0005_base_nav_inbox.diff'):
                subprocess.run(['git', 'apply', str(patches / name)], cwd=tmp, check=True)
            r = subprocess.run(['git', 'apply', '--check', str(patches / '0007_base_agent_card.diff')],
                               cwd=tmp, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            subprocess.run(['git', 'apply', str(patches / '0007_base_agent_card.diff')], cwd=tmp, check=True)
            text = (pathlib.Path(tmp) / 'templates' / 'base.html').read_text()
            self.assertEqual(text.count('{% agent_card %}'), 1)
