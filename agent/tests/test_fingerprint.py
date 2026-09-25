"""External-change fingerprint and V5 (Phase 1.1 decision 3; Phase 1.2 items 2-4).
Engines are real. Tests only simulate a person's edit (mapping, setting, deleted upload)
the way the core UI stores it, and simulate a number shift by editing agent tables only."""
from django.test import TestCase, TransactionTestCase

from agent import validate
from agent.canonical import sha256_of
from agent.checks import baseline_groups
from agent.diagnose import diagnose
from agent.fingerprint import SETTING_KEYS, VERSION, diff, fingerprint, fingerprint_sha
from agent.models import (
    AgentAction, AgentAuthorisation, AgentConfig, AgentProposal, AgentRun, ScheduleStatus,
    ScopeState, SummarySnapshot,
)
from agent.service import ensure_service_user
from agent.tools.reconcile import reconcile_scope
from core.models import BrandMapping, MonitoringData, SystemSetting

from . import factories as f


def scope(acc, month=f.MONTH):
    return ScopeState.objects.get_or_create(account=acc, channel=f.CHANNEL, month=month)[0]


def snapshot(sc, s, fp, sha='old'):
    return SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number=s.schedule_number,
                                          data={}, sha256=sha, fingerprint=fp,
                                          fingerprint_sha256=fingerprint_sha(fp) if fp else '')


def scoped(acc=None):
    """A schedule with a Nexus row and a Nexus LMRB row in this scope."""
    acc = acc or f.account()
    s = f.schedule(acc)
    f.row(acc, s, brand='Nexus')
    f.lmrb(acc, day=10, theme='Nexus (30)(Sin)')
    return acc, s


def monitoring(acc, channel=f.CHANNEL):
    return MonitoringData.objects.create(account=acc, data_type='mediawatch', channel=channel,
                                         file='monitoring/x.xlsx', original_filename='x.xlsx')


class FingerprintTest(TestCase):
    def test_contents(self):
        acc, s = scoped()
        m = f.mapping(acc)
        md = monitoring(acc)
        monitoring(acc, channel='Derana TV')                       # other channel: not listed
        fp = fingerprint(scope(acc))
        self.assertEqual(fp['version'], VERSION)
        self.assertEqual(fp['brand_mappings'][str(m.id)]['tc_theme'], 'NEXUS 30')
        self.assertEqual(set(fp['schedules'][str(s.id)]),
                         {'channel', 'month', 'version', 'is_superseded', 'is_locked'})
        self.assertEqual(set(fp['settings']), set(SETTING_KEYS))
        self.assertEqual(fp['monitoring_data'], [md.id])
        self.assertEqual(fp['lmrb_count'], 1)
        self.assertEqual(diff(fp, fingerprint(scope(acc))), {})       # stable

    def test_person_edits_mapping_in_this_scope_is_explained(self):
        acc, s = scoped()
        m = f.mapping(acc)
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        BrandMapping.objects.filter(pk=m.pk).update(tc_theme='NEXUS 30 NEW')    # a person's edit
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertTrue(c.ok)
        self.assertEqual(c.detail['explained_by'], ['external_change'])
        self.assertEqual(c.detail['relevant_diff']['brand_mappings']['changed'],
                         {str(m.id): {'tc_theme': ['NEXUS 30', 'NEXUS 30 NEW']}})

    def test_mapping_edit_for_brand_on_other_channel_does_not_explain(self):
        acc, s = scoped()
        derana = f.schedule(acc, number='301', channel='Derana TV')
        f.row(acc, derana, brand='Krest')                              # Krest airs on Derana only
        f.lmrb(acc, day=10, theme='Krest (30)(Sin)', channel='Derana TV')
        other = f.mapping(acc, brand='Krest', theme='Krest (30)(Sin)', tc='KREST 30')
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        BrandMapping.objects.filter(pk=other.pk).update(tc_theme='KREST 30 NEW')
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertFalse(c.ok)
        self.assertEqual(c.detail['explained_by'], [])
        self.assertIn(str(other.id), c.detail['fingerprint_diff']['brand_mappings']['changed'])   # kept, info only
        self.assertEqual(c.detail['relevant_diff'], {})

    def test_wildcard_theme_mapping_matching_scope_lmrb_explains(self):
        acc, s = scoped()
        f.lmrb(acc, day=11, theme='Expo 2025_1 (30)(Sin)')
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        m = f.mapping(acc, brand='Expo Brand', theme='expo 2025*', tc='')    # brand not in schedule
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertTrue(c.ok)
        self.assertEqual(c.detail['relevant_diff']['brand_mappings']['added'], [str(m.id)])

    def test_alias_setting_change_is_explained(self):
        acc, s = scoped()
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        SystemSetting.objects.create(key='tc_extra_theme_aliases', value='Spot Name', label='x')
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertTrue(c.ok)
        self.assertEqual(c.detail['relevant_diff']['settings']['changed'],
                         {'tc_extra_theme_aliases': ['', 'Spot Name']})

    def test_deleting_monitoring_file_is_explained_and_names_the_id(self):
        acc, s = scoped()
        keep, gone = monitoring(acc), monitoring(acc)
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        MonitoringData.objects.filter(pk=gone.pk).delete()      # a person deletes an upload
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertTrue(c.ok)
        self.assertEqual(c.detail['relevant_diff']['monitoring_data'],
                         {'added': [], 'removed': [gone.id], 'changed': {}})

    def test_no_change_at_all_is_unexplained(self):
        acc, s = scoped()
        sc = scope(acc)
        snapshot(sc, s, fingerprint(sc))
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertFalse(c.ok)
        self.assertEqual(c.detail['explained_by'], [])

    def test_baseline_no_prior_snapshot(self):
        acc, s = scoped()
        sc = scope(acc)
        c = validate.v5(sc, s, 'x', fingerprint(sc))
        self.assertTrue(c.ok)
        self.assertEqual(c.detail['baseline'], 'no_prior_snapshot')

    def test_baseline_pre_1_1_snapshot_without_fingerprint(self):
        acc, s = scoped()
        sc = scope(acc)
        snapshot(sc, s, {})                                            # saved before 1.1
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertTrue(c.ok)
        self.assertEqual((c.detail['baseline'], c.detail['changed']), ('baseline_no_fingerprint', True))
        snapshot(sc, s, {'manual_matches': [1]}, sha='older')         # Phase 1.1 layout: same
        self.assertEqual(validate.v5(sc, s, 'new', fingerprint(sc)).detail['baseline'],
                         'baseline_no_fingerprint')


    def test_authorised_schedule_is_never_baselined(self):
        """Guardian note (1.2): outside a fully AUTHORISED scope too, an authorised
        schedule with an old-layout snapshot and changed numbers stays unexplained."""
        acc, s = scoped()
        sc = scope(acc)
        snap = snapshot(sc, s, {})
        AgentAuthorisation.objects.create(schedule=s, snapshot=snap, snapshot_sha256='old',
                                          authorised_by=f.user())
        c = validate.v5(sc, s, 'new', fingerprint(sc))
        self.assertFalse(c.ok)
        self.assertNotIn('baseline', c.detail)
        self.assertTrue(validate.v5(sc, s, 'old', fingerprint(sc)).ok)     # unchanged numbers: fine


def enable():
    c = AgentConfig.get_solo()
    c.enabled, c.autonomy_level, c.upload_debounce_minutes = True, 1, 0
    c.save()
    return ensure_service_user()[0]


class FingerprintReconcileTest(TransactionTestCase):
    def test_first_run_is_baseline_not_needs_human(self):
        acc, s = f.full_scope()
        enable()
        sc = scope(acc)
        res = reconcile_scope(sc.id)
        self.assertEqual(res['status'], 'ok')
        self.assertEqual(res['baselines'], {str(s.id): 'no_prior_snapshot'})
        sc.refresh_from_db()
        self.assertNotEqual(sc.state, 'NEEDS_HUMAN')
        self.assertEqual(ScheduleStatus.objects.get(schedule=s).baseline_reason, 'no_prior_snapshot')
        self.assertTrue([x for x in diagnose(sc) if x.code == 'BASELINE' and x.severity == 'info'])
        # second run: no longer a baseline
        reconcile_scope(sc.id)
        self.assertEqual(ScheduleStatus.objects.get(schedule=s).baseline_reason, '')

    def test_pre_1_1_snapshot_with_changed_numbers_is_baseline_and_grouped(self):
        a1, s1 = f.full_scope()
        a2, s2 = f.full_scope(acc=f.account('Other'))
        enable()
        for acc, s in ((a1, s1), (a2, s2)):
            snapshot(scope(acc), s, {}, sha='pre-1.1 numbers')          # no fingerprint, numbers differ
        for acc in (a1, a2):
            res = reconcile_scope(scope(acc).id)
            self.assertEqual(res['status'], 'ok')
            self.assertNotEqual(ScopeState.objects.get(pk=scope(acc).id).state, 'NEEDS_HUMAN')
        groups = baseline_groups()
        self.assertEqual(len(groups), 1)                                 # one grouped card, not per scope
        self.assertEqual((groups[0]['reason'], groups[0]['schedules'], groups[0]['scopes']),
                         ('baseline_no_fingerprint', 2, 2))
        self.assertEqual(sorted(groups[0]['accounts']), ['Keells', 'Other'])

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
        ext = run.detail['external_changes'][str(s.id)]
        self.assertIn(str(m.id), ext['relevant']['brand_mappings']['changed'])
        self.assertIn(str(m.id), ext['account_wide']['brand_mappings']['changed'])

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
