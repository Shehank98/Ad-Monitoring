"""External-change fingerprint and V5 (Phase 1.1, owner decision 3).
Engines are real. Tests only simulate a person's edit (mapping, setting) the way the core
UI would store it, and simulate a number shift by editing agent tables only."""
from django.test import TestCase, TransactionTestCase

from agent import validate
from agent.canonical import sha256_of
from agent.fingerprint import SETTING_KEYS, diff, fingerprint, fingerprint_sha
from agent.models import (
    AgentAction, AgentAuthorisation, AgentConfig, AgentProposal, AgentRun, ScopeState, SummarySnapshot,
)
from agent.service import ensure_service_user
from agent.tools.reconcile import reconcile_scope
from core.models import BrandMapping, SystemSetting

from . import factories as f


def scope(acc):
    return ScopeState.objects.get_or_create(account=acc, channel=f.CHANNEL, month=f.MONTH)[0]


def snapshot(sc, s, fp, sha='old'):
    return SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number=s.schedule_number,
                                          data={}, sha256=sha, fingerprint=fp,
                                          fingerprint_sha256=fingerprint_sha(fp))


class FingerprintTest(TestCase):
    def test_contents(self):
        acc = f.account()
        s = f.schedule(acc)
        m = f.mapping(acc)
        fp = fingerprint(scope(acc))
        self.assertEqual(fp['brand_mappings'][str(m.id)]['tc_theme'], 'NEXUS 30')
        self.assertEqual(set(fp['schedules'][str(s.id)]), {'version', 'is_superseded', 'is_locked'})
        self.assertEqual(set(fp['settings']), set(SETTING_KEYS))
        self.assertEqual(diff(fp, fingerprint(scope(acc))), {})       # stable

    def test_person_edits_mapping_is_explained_and_diff_names_the_mapping(self):
        acc = f.account()
        s = f.schedule(acc)
        m = f.mapping(acc)
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        BrandMapping.objects.filter(pk=m.pk).update(tc_theme='NEXUS 30 NEW')    # a person's edit
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertTrue(c.ok)
        self.assertEqual(c.detail['explained_by'], ['external_change'])
        self.assertEqual(c.detail['fingerprint_diff']['brand_mappings']['changed'],
                         {str(m.id): {'tc_theme': ['NEXUS 30', 'NEXUS 30 NEW']}})

    def test_alias_setting_change_is_explained(self):
        acc = f.account()
        s = f.schedule(acc)
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        SystemSetting.objects.create(key='tc_extra_theme_aliases', value='Spot Name', label='x')
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertTrue(c.ok)
        self.assertEqual(c.detail['fingerprint_diff']['settings']['changed'],
                         {'tc_extra_theme_aliases': ['', 'Spot Name']})

    def test_no_change_at_all_is_unexplained(self):
        acc = f.account()
        s = f.schedule(acc)
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertFalse(c.ok)
        self.assertEqual(c.detail['explained_by'], [])


def enable():
    c = AgentConfig.get_solo()
    c.enabled, c.autonomy_level, c.upload_debounce_minutes = True, 1, 0
    c.save()
    return ensure_service_user()[0]


class FingerprintReconcileTest(TransactionTestCase):
    def test_snapshot_stores_fingerprint_and_second_run_is_quiet(self):
        acc, s = f.full_scope()
        enable()
        sc = scope(acc)
        self.assertEqual(reconcile_scope(sc.id)['status'], 'ok')
        snap = SummarySnapshot.objects.get(scope=sc, schedule=s)
        self.assertEqual(snap.fingerprint_sha256, fingerprint_sha(snap.fingerprint))
        self.assertEqual(snap.fingerprint_sha256, fingerprint_sha(fingerprint(sc)))
        res = reconcile_scope(sc.id)
        self.assertEqual(res['status'], 'ok')
        self.assertEqual(res['external_changes'], {})

    def test_numbers_shift_with_no_change_needs_human(self):
        acc, s = f.full_scope()
        enable()
        sc = scope(acc)
        reconcile_scope(sc.id)
        SummarySnapshot.objects.filter(scope=sc).update(sha256='shifted')   # numbers moved, nothing explains it
        res = reconcile_scope(sc.id)
        self.assertEqual(res['status'], 'unexplained_change')
        sc.refresh_from_db()
        self.assertEqual(sc.state, 'NEEDS_HUMAN')

    def test_mapping_edit_between_runs_is_explained_and_saved_in_run(self):
        acc, s = f.full_scope()
        enable()
        sc = scope(acc)
        reconcile_scope(sc.id)
        m = BrandMapping.objects.get(account=acc)
        BrandMapping.objects.filter(pk=m.pk).update(maponline_theme='Nexus MO')
        SummarySnapshot.objects.filter(scope=sc).update(sha256='shifted')
        res = reconcile_scope(sc.id)
        self.assertEqual(res['status'], 'ok')
        run = AgentRun.objects.filter(scope=sc, kind='scope').latest('id')
        self.assertIn(str(m.id), run.detail['external_changes'][str(s.id)]['brand_mappings']['changed'])

    def test_authorised_schedule_change_creates_amendment_proposal(self):
        acc, s = f.full_scope()
        svc = enable()
        sc = scope(acc)
        reconcile_scope(sc.id)
        snap = SummarySnapshot.objects.get(scope=sc, schedule=s)
        AgentAuthorisation.objects.create(schedule=s, snapshot=snap, snapshot_sha256='authorised-old',
                                          authorised_by=f.user())
        actions = AgentAction.objects.count()
        m = BrandMapping.objects.get(account=acc)
        BrandMapping.objects.filter(pk=m.pk).update(maponline_theme='Nexus MO')   # a person's edit
        res = reconcile_scope(sc.id)
        self.assertEqual(res['status'], 'skipped')                  # frozen: no engine runs
        self.assertEqual(AgentAction.objects.count(), actions)
        p = AgentProposal.objects.get(kind='amendment')
        self.assertEqual((p.schedule_id, p.status, p.actor_id), (s.id, 'open', svc.id))
        self.assertEqual(p.before['sha256'], 'authorised-old')
        self.assertEqual(p.after['sha256'], sha256_of(p.after['summary']))
        self.assertIn(str(m.id), p.evidence['fingerprint_diff']['brand_mappings']['changed'])
        reconcile_scope(sc.id)                                      # no duplicate proposal
        self.assertEqual(AgentProposal.objects.filter(kind='amendment').count(), 1)

    def test_authorised_schedule_unexplained_change_needs_human(self):
        acc, s = f.full_scope()
        enable()
        sc = scope(acc)
        reconcile_scope(sc.id)
        snap = SummarySnapshot.objects.get(scope=sc, schedule=s)
        AgentAuthorisation.objects.create(schedule=s, snapshot=snap, snapshot_sha256='authorised-old',
                                          authorised_by=f.user())
        res = reconcile_scope(sc.id)
        self.assertEqual(res['status'], 'unexplained_change')
        self.assertFalse(AgentProposal.objects.exists())
        sc.refresh_from_db()
        self.assertEqual(sc.state, 'NEEDS_HUMAN')
