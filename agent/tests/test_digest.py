"""Phase 3 g + d + UI: daily digest, core fingerprint, audit agent-effect section, overview cards."""
import datetime

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from agent import core_fingerprint, digest
from agent.core_audit import agent_effect, render_agent_effect
from agent.models import AgentConfig, AgentRun, NotificationLog, PendingEffect, ScopeState, SummarySnapshot
from agent.service import ensure_service_user
from core.models import SystemSetting

from . import factories as f

TZ = timezone.get_current_timezone()
MORNING = timezone.make_aware(datetime.datetime(2025, 2, 10, 7, 45), TZ)
EARLY = timezone.make_aware(datetime.datetime(2025, 2, 10, 7, 0), TZ)
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'


def smtp_on():
    for k, v in {'email_enabled': '1', 'email_host': 'smtp.example.lk', 'email_host_user': 'agent@example.lk',
                 'email_from_address': 'agent@example.lk'}.items():
        SystemSetting.objects.update_or_create(key=k, defaults={'value': v})


@override_settings(AGENT_DIGEST_EMAIL_BACKEND=LOCMEM)
class DigestTest(TestCase):
    def setUp(self):
        ensure_service_user()
        self.admin = f.user(role='admin', email='boss@x.lk')
        self.planner = f.user(role='planner', email='plan@x.lk')

    def test_not_before_digest_time(self):
        self.assertEqual(digest.maybe_send(EARLY)['reason'], 'before_digest_time')
        self.assertFalse(NotificationLog.objects.exists())

    def test_email_off_logs_only(self):
        res = digest.maybe_send(MORNING)
        self.assertEqual((res['sent'], res['logged']), (0, 1))
        log = NotificationLog.objects.get()
        self.assertEqual((log.recipient, log.via, log.status), (self.admin, 'log', 'logged'))
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn('daily digest', AgentRun.objects.get(kind='digest').detail['text'])

    def test_sent_once_per_day_to_admins_only(self):
        smtp_on()
        res = digest.maybe_send(MORNING)
        self.assertEqual(res['sent'], 1)
        self.assertEqual([m.to for m in mail.outbox], [['boss@x.lk']])        # no planner, no service user
        again = digest.maybe_send(MORNING + datetime.timedelta(minutes=15))
        self.assertEqual(again['sent'], 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(NotificationLog.objects.filter(dedupe_key=f'digest|2025-02-10|{self.admin.id}').exists())

    def test_named_recipients(self):
        other = f.user(role='super_admin', email='sa@x.lk')
        cfg = AgentConfig.get_solo()
        cfg.digest_recipients.set([other, self.planner])      # planner is filtered out
        self.assertEqual(digest.recipients(cfg), [other])

    def test_effects_top_20_with_rest_count(self):
        acc, s = f.full_scope()
        sc = ScopeState.objects.create(account=acc, channel=s.channel, month=s.month)
        snap = SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101', kind='observed',
                                              data={}, sha256='x')
        rows = [{'section': 'commercial', 'programme': '', 'product': f'B{i:02d}', 'dur': 30,
                 'deltas': {'aired': i + 1, 'missed': -(i + 1)}, 'observed': {}, 'shadow': {}} for i in range(25)]
        rows.append({'section': 'commercial', 'programme': '', 'product': 'Tiny', 'dur': 30,
                     'deltas': {'third_party': 1}, 'observed': {}, 'shadow': {}})    # no aired/missed change
        PendingEffect.objects.create(scope=sc, schedule=s, observed=snap, shadow=snap, by_brand=rows, max_abs=25)
        ctx = digest.build(timezone.now())
        self.assertEqual(len(ctx['effects']), 20)
        self.assertEqual(ctx['effects_more'], 5)
        self.assertEqual(ctx['effects'][0]['product'], 'B24')
        text = digest.render_to_string('agent/_digest_email.txt', ctx)
        self.assertIn('and 5 more', text)
        self.assertIn('Aired +25', text)


class CoreFingerprintTest(TestCase):
    def test_covers_every_core_and_accounts_table(self):
        fp = core_fingerprint.take()
        self.assertEqual(fp['label'], 'detection, not proof')
        self.assertEqual(set(fp['tables']) | set(fp['errors']), set(core_fingerprint.tables()))
        for t in ('core_schedule', 'core_lmrbrow', 'core_tcrow', 'accounts_user'):
            self.assertIn(t, fp['tables'])

    def test_diff_names_changed_tables(self):
        a = core_fingerprint.take()
        f.account('Changed')
        b = core_fingerprint.take()
        self.assertEqual(list(core_fingerprint.diff(a, b)), ['core_account'])

    def test_audit_agent_effect_section(self):
        self.assertIn('No completed shadow window yet.', render_agent_effect(agent_effect()))
        AgentRun.objects.create(kind='shadow_window', status='ok', detail={
            'night': '2025-02-10', 'dry_runs': 3, 'used_seconds': 12.5, 'agent_actions_in_window': 0,
            'human_actions_in_window': 1, 'diff': {'core_tcrow': {}}, 'start': {'errors': {}}, 'end': {'errors': {}}})
        md = render_agent_effect(agent_effect())
        self.assertIn('Detection, not proof', md)
        self.assertIn('| 2025-02-10 | closed | 3 | 12.5 | 0 | 1 | core_tcrow | none | none |', md)


class FingerprintNoiseTest(TestCase):
    """Phase 3.1 T9."""

    def test_groups_and_timings(self):
        g = core_fingerprint.group_tables(['core_tcrow', 'core_schedulerow', 'core_periodsponsorshipmatch',
                                           'core_tclmrbmatch', 'accounts_user', 'core_auditlog'])
        self.assertEqual(g['reconciliation'], ['core_periodsponsorshipmatch', 'core_schedulerow',
                                               'core_tclmrbmatch', 'core_tcrow'])
        self.assertEqual(g['activity'], ['accounts_user', 'core_auditlog'])
        self.assertEqual(core_fingerprint.group_tables([]), {'reconciliation': [], 'activity': []})
        fp = core_fingerprint.take()
        self.assertEqual(set(fp['timing_ms']), set(core_fingerprint.tables()))
        self.assertIn('total_ms', fp)

    def test_last_login_does_not_change_the_hash(self):
        u = f.user(role='admin', email='login@x.lk')
        a = core_fingerprint.take()
        from accounts.models import User
        User.objects.filter(pk=u.pk).update(last_login=timezone.now())
        b = core_fingerprint.take()
        self.assertNotIn('accounts_user', core_fingerprint.diff(a, b))
        User.objects.filter(pk=u.pk).update(role='planner')
        c = core_fingerprint.take()
        if core_fingerprint.connection.vendor == 'postgresql':           # hashes exist only on PostgreSQL
            self.assertIn('accounts_user', core_fingerprint.diff(b, c))

    def test_audit_shows_both_groups_and_pending(self):
        now = timezone.now()
        AgentRun.objects.create(kind='shadow_window', status='running', detail={
            'night': '2025-02-11', 'window_start': (now - datetime.timedelta(hours=6)).isoformat(),
            'window_end': (now - datetime.timedelta(hours=2)).isoformat(), 'dry_runs': 1, 'used_seconds': 1})
        AgentRun.objects.create(kind='shadow_window', status='ok', detail={
            'night': '2025-02-10', 'dry_runs': 3, 'used_seconds': 12.5, 'agent_actions_in_window': 0,
            'human_actions_in_window': 0, 'diff': {'core_tcrow': {}, 'core_auditlog': {}},
            'start': {'errors': {}, 'total_ms': 5}, 'end': {'errors': {}, 'total_ms': 6}})
        md = render_agent_effect(agent_effect())
        self.assertIn('| 2025-02-11 | pending |', md)
        self.assertIn('| core_tcrow | core_auditlog | none | 5 / 6 |', md)


class UiTest(TestCase):
    def setUp(self):
        self.admin = f.user(role='admin', email='boss@x.lk')
        self.client.force_login(self.admin)

    def test_overview_quality_and_cycle_cards(self):
        acc, s = f.full_scope()
        self.admin.accounts.add(acc)
        r = self.client.get(f'/dashboard/agent/?month={s.month}')
        self.assertContains(r, 'Diagnosis quality')
        self.assertContains(r, 'inferred')
        self.assertContains(r, 'Agent cycle')

    def test_scope_detail_shows_pending_effect(self):
        acc, s = f.full_scope()
        self.admin.accounts.add(acc)
        sc = ScopeState.objects.create(account=acc, channel=s.channel, month=s.month)
        snap = SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101', kind='observed',
                                              data={}, sha256='x')
        PendingEffect.objects.create(scope=sc, schedule=s, observed=snap, shadow=snap, max_abs=2, by_brand=[
            {'section': 'commercial', 'programme': '', 'product': 'Nexus', 'dur': 30, 'deltas': {'aired': 2},
             'observed': {'planned': 2, 'aired': 0, 'third_party': 0, 'extra': 0, 'missed': 2},
             'shadow': {'planned': 2, 'aired': 2, 'third_party': 2, 'extra': 0, 'missed': 0}}])
        r = self.client.get(f'/dashboard/agent/scope/?account_id={acc.id}&channel={s.channel}&month={s.month}')
        self.assertContains(r, 'Pending effect')
        self.assertContains(r, 'rolled back')

    def test_settings_form_saves_phase3_fields(self):
        cfg = AgentConfig.get_solo()
        from agent.views import _snap_config
        data = {k: v for k, v in _snap_config(cfg).items() if v is not None}
        data.update(what='config', shadow_window_start='00:30', shadow_window_end='04:30', shadow_budget_seconds=900,
                    digest_time='08:00', digest_recipients=[self.admin.id], enabled='')
        data = {k: v for k, v in data.items() if v is not False}
        r = self.client.post('/dashboard/agent/config/', data)
        self.assertEqual(r.status_code, 302, getattr(r, 'context', None) and r.context['form'].errors)
        cfg.refresh_from_db()
        self.assertEqual((cfg.shadow_window_start, cfg.shadow_budget_seconds), (datetime.time(0, 30), 900))
        self.assertEqual(list(cfg.digest_recipients.all()), [self.admin])
