"""Phase 3.1: owner decisions on codes, T1 (V5_UNEXPLAINED persists), T2 (NO_BRAND_MAPPING),
T5 (RECONCILE_PENDING)."""
import datetime
from datetime import timedelta

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from agent import cycle, ledger, measure
from agent.diagnose import Finding, diagnose
from agent.fingerprint import fingerprint, scope_context
from agent.models import AgentAction, AgentProposal, FindingLedger, PendingEffect, ScopeState, SummarySnapshot
from agent.service import ensure_service_user
from core.models import BrandMapping, TCRow

from . import factories as f
from .test_cycle import DAY, NIGHT, age_uploads, enable


class CodeDecisionsTest(TestCase):
    def test_owner_code_lists(self):
        for c in ('TC_NO_ROWS', 'LMRB_THEME_NO_ROWS', 'SPONSORSHIP_NOT_RUN', 'NO_BRAND_MAPPING'):
            self.assertIn(c, ledger.ACTIONABLE)
        self.assertIn('LMRB_MULTI_FLAG', ledger.INFO_ONLY)
        self.assertEqual(ledger.MAPPING_GROUP, ('NO_TC_MAPPING', 'LMRB_THEME_NO_ROWS', 'NO_BRAND_MAPPING'))
        from agent.labels import CAUSE_CODES
        for c in ('BASELINE', 'V5_UNEXPLAINED', 'RECONCILE_PENDING'):
            self.assertNotIn(c, CAUSE_CODES)

    def test_baseline_never_enters_the_ledger(self):
        acc, s = f.full_scope()
        sc = ScopeState.objects.create(account=acc, channel=s.channel, month=s.month)
        fp = fingerprint(sc)
        ledger.observe(sc, [Finding('BASELINE', 'first run', tier=0, evidence={'schedule_id': s.id})], fp, fp,
                       scope_context(sc))
        self.assertFalse(FindingLedger.objects.exists())


class NoBrandMappingTest(TestCase):
    """T2."""

    def test_no_tc_mapping_fires_for_brand_with_no_mapping_row(self):
        acc, s = f.full_scope()
        f.row(acc, s, brand='Ghost', day=11)
        sc = ScopeState(account=acc, channel=s.channel, month=s.month)
        codes = {(x.code, x.brand) for x in diagnose(sc)}
        self.assertIn(('NO_TC_MAPPING', 'Ghost'), codes)
        self.assertIn(('NO_BRAND_MAPPING', 'Ghost'), codes)

    def test_no_brand_mapping_when_only_the_lmrb_theme_is_missing(self):
        acc, s = f.full_scope()
        BrandMapping.objects.filter(account=acc, brand='Nexus').update(theme='')
        sc = ScopeState(account=acc, channel=s.channel, month=s.month)
        fs = [x for x in diagnose(sc) if x.brand == 'Nexus']
        codes = {x.code for x in fs}
        self.assertIn('NO_BRAND_MAPPING', codes)
        self.assertNotIn('NO_TC_MAPPING', codes)          # TC theme is mapped: before T2 nothing fired
        self.assertTrue(next(x for x in fs if x.code == 'NO_BRAND_MAPPING').evidence['brand_mapping_row_exists'])


class V5PersistsTest(TestCase):
    """T1."""

    def setUp(self):
        ensure_service_user()
        enable()
        self.acc, self.s = f.full_scope()
        age_uploads()
        self.admin = f.user(role='admin', email='boss@x.lk', accounts=[self.acc])

    def scope(self):
        return ScopeState.objects.get(account=self.acc, channel=self.s.channel, month=self.s.month)

    def redo(self):
        from agent.models import AgentRun
        AgentRun.objects.filter(kind='scope').update(started_at=timezone.now() - timedelta(hours=7))
        return cycle.run_cycle(now=DAY)

    def make_unexplained(self):
        cycle.run_cycle(now=DAY)
        TCRow.objects.update(is_schedule_matched=True, is_lmrb_confirmed=True)   # not in the fingerprint
        self.redo()

    def ack(self, row, cause='core_bug', note='checked'):
        self.client.force_login(self.admin)
        return self.client.post(f'/dashboard/agent/finding/{row.id}/acknowledge/', {'root_cause': cause, 'note': note})

    def test_row_with_evidence_and_proposal(self):
        self.make_unexplained()
        row = FindingLedger.objects.get(code='V5_UNEXPLAINED')
        self.assertTrue(row.actionable and row.open)
        self.assertEqual(row.schedule_id, self.s.id)
        self.assertEqual(row.evidence['fingerprint_diff'], {})
        self.assertTrue(row.evidence['by_brand'])
        self.assertEqual(row.evidence['by_brand'][0]['deltas'].get('aired'), 2)
        self.assertEqual((row.proposal.kind, row.proposal.tier, row.proposal.apply_payload), ('finding', 4, {}))

    def test_next_cycle_does_not_clear_it(self):
        self.make_unexplained()
        self.redo()
        self.redo()
        sc = self.scope()
        self.assertEqual((sc.state, sc.reason), ('NEEDS_HUMAN', 'unexplained_change'))
        self.assertEqual(FindingLedger.objects.filter(code='V5_UNEXPLAINED').count(), 1)   # not re-flagged
        self.assertTrue(FindingLedger.objects.get(code='V5_UNEXPLAINED').open)

    def test_acknowledging_clears_it(self):
        self.make_unexplained()
        row = FindingLedger.objects.get(code='V5_UNEXPLAINED')
        r = self.ack(row, 'fingerprint_gap', 'TC flags are not in the fingerprint')
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual((row.open, row.resolution), (False, 'fingerprint_gap'))
        self.assertNotEqual(self.scope().state, 'NEEDS_HUMAN')
        act = AgentAction.objects.get(action_type='v5_acknowledge')
        self.assertEqual((act.actor_kind, act.target_model), ('human', 'agent.FindingLedger'))
        self.assertEqual(measure.v5_criterion()['count'], 1)          # fingerprint_gap still counts
        self.redo()
        self.assertNotEqual(self.scope().state, 'NEEDS_HUMAN')

    def test_note_and_cause_are_required(self):
        self.make_unexplained()
        row = FindingLedger.objects.get(code='V5_UNEXPLAINED')
        self.ack(row, 'core_bug', '')
        self.ack(row, 'whatever', 'x')
        row.refresh_from_db()
        self.assertTrue(row.open)

    def test_stays_needs_human_until_every_row_is_acknowledged(self):
        self.make_unexplained()
        first = FindingLedger.objects.get(code='V5_UNEXPLAINED')
        TCRow.objects.update(is_lmrb_confirmed=False)                 # a second unexplained change
        self.redo()
        rows = list(FindingLedger.objects.filter(code='V5_UNEXPLAINED', open=True).order_by('id'))
        self.assertEqual(len(rows), 2)
        self.ack(rows[0])
        self.assertEqual(self.scope().state, 'NEEDS_HUMAN')
        self.ack(rows[1])
        self.assertNotEqual(self.scope().state, 'NEEDS_HUMAN')
        self.assertEqual(first.id, rows[0].id)

    def test_digest_lists_open_rows_with_age(self):
        self.make_unexplained()
        from agent import digest
        ctx = digest.build(timezone.now())
        self.assertEqual(len(ctx['v5_unexplained']), 1)
        self.assertIn('age_days', ctx['v5_unexplained'][0])


class ReconcilePendingUnitTest(TestCase):
    """T5, with fabricated snapshots."""

    def setUp(self):
        self.acc, self.s = f.full_scope()
        self.sc = ScopeState.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month)

    def snap(self, kind, sha):
        return SummarySnapshot.objects.create(scope=self.sc, schedule=self.s, schedule_number='101', kind=kind,
                                              data={}, sha256=sha)

    def effect(self, obs, sh, max_abs):
        return PendingEffect.objects.create(scope=self.sc, schedule=self.s, observed=obs, shadow=sh, max_abs=max_abs,
                                            by_brand=[{'product': 'Nexus', 'deltas': {'aired': max_abs}}])

    def test_opens_and_closes_when_people_reconcile(self):
        o1, sh = self.snap('observed', 'a'), self.snap('shadow', 'b')
        self.effect(o1, sh, 2)
        t0 = timezone.now()
        self.assertEqual(ledger.reconcile_pending(self.sc, {self.s.id: o1}, now=t0)['opened'], 1)
        row = FindingLedger.objects.get(code='RECONCILE_PENDING')
        self.assertEqual(row.text, 'Running reconciliation would change these numbers.')
        self.assertEqual(row.evidence['pending_effect_id'], PendingEffect.objects.get().id)
        self.assertEqual(AgentProposal.objects.filter(status='open').count(), 1)
        # a person ran the core reconcile: the next observed snapshot equals the shadow
        o2 = self.snap('observed', 'b')
        ledger.reconcile_pending(self.sc, {self.s.id: o2}, now=t0 + timedelta(hours=5))
        row.refresh_from_db()
        self.assertEqual((row.open, row.resolution), (False, 'reconciled_by_human'))
        self.assertEqual(row.resolution_evidence['seconds_to_resolve'], 18000.0)
        self.assertEqual(measure.reconcile_pending_stats()['median_hours'], 5.0)
        self.assertEqual(AgentProposal.objects.get().status, 'superseded')
        # the same effect never reopens it
        ledger.reconcile_pending(self.sc, {self.s.id: o2}, now=t0 + timedelta(hours=6))
        self.assertFalse(FindingLedger.objects.get(code='RECONCILE_PENDING').open)

    def test_small_or_absent_effect_does_not_open(self):
        o1, sh = self.snap('observed', 'a'), self.snap('shadow', 'b')
        self.effect(o1, sh, 0)
        ledger.reconcile_pending(self.sc, {self.s.id: o1})
        self.assertFalse(FindingLedger.objects.exists())

    def test_new_zero_effect_closes_as_no_longer_pending(self):
        o1, sh = self.snap('observed', 'a'), self.snap('shadow', 'b')
        self.effect(o1, sh, 3)
        ledger.reconcile_pending(self.sc, {self.s.id: o1})
        o2, sh2 = self.snap('observed', 'c'), self.snap('shadow', 'c')
        self.effect(o2, sh2, 0)
        ledger.reconcile_pending(self.sc, {self.s.id: o2})
        self.assertEqual(FindingLedger.objects.get().resolution, 'no_longer_pending')

    def test_cycle_codes_never_close_by_absence(self):
        o1, sh = self.snap('observed', 'a'), self.snap('shadow', 'b')
        self.effect(o1, sh, 2)
        ledger.reconcile_pending(self.sc, {self.s.id: o1})
        fp = fingerprint(self.sc)
        for _ in range(3):
            ledger.observe(self.sc, [], fp, fp, scope_context(self.sc))
        self.assertTrue(FindingLedger.objects.get(code='RECONCILE_PENDING').open)


@override_settings(AGENT_ALLOW_SQLITE_DRY_RUN=True)
class ReconcilePendingCycleTest(TransactionTestCase):
    def test_night_cycle_opens_reconcile_pending(self):
        ensure_service_user()
        enable()
        acc, s = f.full_scope()                   # TC rows not reconciled yet: a run would add Aired
        age_uploads()
        cycle.run_cycle(now=NIGHT)
        row = FindingLedger.objects.get(code='RECONCILE_PENDING')
        self.assertTrue(row.open and row.actionable)
        self.assertGreaterEqual(row.evidence['max_abs'], 1)


class NightlyCloseTest(TransactionTestCase):
    """T3."""

    def setUp(self):
        ensure_service_user()
        enable()
        self.acc, self.s = f.full_scope()
        age_uploads()

    def open_window(self):
        from agent.models import AgentRun
        now = timezone.now()
        return AgentRun.objects.create(kind='shadow_window', status='running', detail={
            'night': '2025-02-10', 'window_start': (now - timedelta(hours=5)).isoformat(),
            'window_end': (now - timedelta(minutes=30)).isoformat(), 'used_seconds': 0, 'dry_runs': 0,
            'start': __import__('agent.core_fingerprint', fromlist=['take']).take()})

    def test_nightly_closes_a_still_open_window(self):
        from django.core.management import call_command
        run = self.open_window()
        call_command('agent_nightly', stdout=__import__('io').StringIO(), stderr=__import__('io').StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, 'ok')
        self.assertIn('end', run.detail)
        self.assertEqual(run.detail['agent_actions_in_window'], 0)

    def test_nightly_and_cycle_racing_close_it_once(self):
        run = self.open_window()
        now = timezone.now()
        self.assertTrue(cycle.close_window(run.id, now))            # the nightly job wins
        end_fp = __import__('agent.models', fromlist=['AgentRun']).AgentRun.objects.get(pk=run.id).detail['end']
        self.assertFalse(cycle.close_window(run.id, now))           # the cycle's attempt is a no-op
        self.assertEqual(cycle.close_windows(now), [])
        run.refresh_from_db()
        self.assertEqual(run.detail['end'], end_fp)                 # end fingerprint not overwritten

    def test_lock_timeout_reports_pending_and_leaves_it_open(self):
        from agent.models import AgentRun, ScopeLockRow
        from django.db import connection, connections
        run = self.open_window()
        other = None
        if connection.vendor == 'postgresql':
            other = connections.create_connection('default')
            with other.cursor() as cur:
                cur.execute('SELECT pg_advisory_lock(%s)', [cycle.CYCLE_LOCK_KEY])
        else:
            ScopeLockRow.objects.create(key=cycle.CYCLE_LOCK_KEY, owner='cycle:1', acquired_at=timezone.now(),
                                        expires_at=timezone.now() + timedelta(minutes=10))
        try:
            res = cycle.nightly_close(wait_seconds=0.2)
        finally:
            if other is not None:
                with other.cursor() as cur:
                    cur.execute('SELECT pg_advisory_unlock(%s)', [cycle.CYCLE_LOCK_KEY])
                other.close()
            ScopeLockRow.objects.filter(key=cycle.CYCLE_LOCK_KEY).delete()
        self.assertEqual((res['lock'], res['pending']), ('timeout', [run.id]))
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        from agent.core_audit import agent_effect
        self.assertEqual(agent_effect()[0]['status'], 'pending')
        cycle.run_cycle()                                           # the next cycle closes it
        run.refresh_from_db()
        self.assertEqual(run.status, 'ok')
