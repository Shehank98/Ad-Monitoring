"""Phase 3 S2, S3, S4: findings ledger, proposals, feedback, owner labels, measures, labelled eval."""
import datetime
import os
import tempfile
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from agent import ledger, measure
from agent.diagnose import Finding
from agent.fingerprint import fingerprint, scope_context
from agent.labels import apply_label, parse
from agent.management.commands.agent_diagnose_labelled import evaluate
from agent.models import AgentAction, AgentProposal, FindingLedger, ScopeState

from . import factories as f


def unmapped(brand='Nexus', dur=30):
    return Finding('NO_TC_MAPPING', f'{brand} unmapped', tier=3, brand=brand, evidence={'duration': dur})


def info():
    return Finding('SUPERSEDED_ROWS_PRESENT', '1 older version', tier=0, severity='info', evidence={'count': 1})


class LedgerTest(TestCase):
    def setUp(self):
        self.acc, self.s = f.full_scope()
        self.sc = ScopeState.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month)
        self.fp = fingerprint(self.sc)
        self.ctx = scope_context(self.sc)

    def obs(self, findings, fp_old=None, fp_new=None):
        return ledger.observe(self.sc, findings, fp_old or self.fp, fp_new or self.fp, self.ctx)

    def test_key_is_sha256_of_the_s3_fields(self):
        import hashlib
        self.obs([unmapped()])
        row = FindingLedger.objects.get()
        raw = f'{self.sc.id}|none|NO_TC_MAPPING|Nexus|30'
        self.assertEqual(row.key, hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(ledger.ledger_key(self.sc.id, None, 'X', '', None),
                         hashlib.sha256(f'{self.sc.id}|none|X|none|none'.encode()).hexdigest())

    def test_same_key_findings_are_merged(self):
        self.obs([Finding('LOCK_ORPHANED', 'a', sub_code='x'), Finding('LOCK_ORPHANED', 'b', sub_code='y')])
        row = FindingLedger.objects.get()
        self.assertIn('a', row.text)
        self.assertIn('b', row.text)

    def test_closes_only_after_two_consecutive_absences(self):
        self.obs([unmapped()])
        self.obs([])
        row = FindingLedger.objects.get()
        self.assertTrue(row.open)
        self.assertEqual(row.absent_count, 1)
        self.obs([unmapped()])                      # back: absence count resets
        self.assertEqual(FindingLedger.objects.get().absent_count, 0)
        self.obs([])
        self.obs([])
        row = FindingLedger.objects.get()
        self.assertFalse(row.open)
        self.assertEqual(row.resolution, 'self_cleared')
        self.assertEqual(row.reopen_count, 0)

    def test_reopen_count_and_flapping(self):
        for _ in range(2):
            self.obs([unmapped()])
            self.obs([])
            self.obs([])
        self.obs([unmapped()])
        row = FindingLedger.objects.get()
        self.assertTrue(row.open)
        self.assertEqual(row.reopen_count, 2)
        self.assertEqual(measure.quality()['flapping'], 1)

    def test_resolution_mapping_changed(self):
        self.obs([unmapped()])
        f.mapping(self.acc, brand='Nexus', theme='Nexus (30)(Sin)', tc='NEXUS 30 NEW')
        fp2 = fingerprint(self.sc)
        self.obs([], self.fp, fp2)
        self.obs([], fp2, fp2)
        row = FindingLedger.objects.get()
        self.assertEqual(row.resolution, 'mapping_changed')
        self.assertIn('brand_mappings', row.resolution_evidence)

    def test_s4_proposal_only_for_actionable_codes_and_superseded_on_close(self):
        self.obs([unmapped(), info()])
        p = AgentProposal.objects.get()
        self.assertEqual((p.kind, p.tier, p.status, p.apply_payload), ('finding', 4, 'open', {}))
        self.assertEqual(p.evidence['code'], 'NO_TC_MAPPING')
        self.assertFalse(FindingLedger.objects.get(code='SUPERSEDED_ROWS_PRESENT').actionable)
        self.obs([unmapped(), info()])
        self.assertEqual(AgentProposal.objects.count(), 1)          # deduplicated per ledger row
        self.obs([info()])
        self.obs([info()])
        p.refresh_from_db()
        self.assertEqual(p.status, 'superseded')
        self.obs([unmapped()])                                       # reopened -> a new proposal
        self.assertEqual(AgentProposal.objects.filter(status='open').count(), 1)

    def test_s4_code_lists_are_disjoint_and_cover_diagnose(self):
        codes = set(ledger.ACTIONABLE) | set(ledger.INFO_ONLY) | set(ledger.UNCOVERED)
        self.assertEqual(len(codes), len(ledger.ACTIONABLE) + len(ledger.INFO_ONLY) + len(ledger.UNCOVERED))
        import inspect
        import re
        from agent import diagnose
        used = set(re.findall(r"Finding\('([A-Z_]+)'", inspect.getsource(diagnose)))
        self.assertTrue(used <= codes, used - codes)

    def test_human_change_recorded_for_inferred_coverage(self):
        self.obs([unmapped()])
        f.mapping(self.acc, brand='Nexus', theme='Nexus (30)(Sin)', tc='NEXUS 30 NEW')
        stats = self.obs([], self.fp, fingerprint(self.sc))
        self.assertEqual(stats['human_changes'], ['mapping_changed'])
        self.assertEqual(stats['open_before'], ['NO_TC_MAPPING'])


class FeedbackAndLabelsTest(TestCase):
    def setUp(self):
        self.acc, self.s = f.full_scope()
        self.sc = ScopeState.objects.create(account=self.acc, channel=self.s.channel, month=self.s.month)
        fp = fingerprint(self.sc)
        ledger.observe(self.sc, [unmapped(), unmapped('Other')], fp, fp, scope_context(self.sc))
        self.admin = f.user(role='admin', accounts=[self.acc])

    def csv(self, *rows):
        fd, path = tempfile.mkstemp(suffix='.csv')
        with os.fdopen(fd, 'w') as fh:
            fh.write('account,channel,month,schedule_number,cause_code,brand,duration,as_of,note\n')
            for r in rows:
                fh.write(','.join(r) + '\n')
        self.addCleanup(os.remove, path)
        return path

    def test_feedback_is_a_human_gate_write_on_agent_tables(self):
        row = FindingLedger.objects.get(brand='Nexus')
        self.client.force_login(self.admin)
        r = self.client.post(f'/dashboard/agent/finding/{row.id}/feedback/', {'label': 'correct', 'note': 'yes'})
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual((row.label, row.label_source, row.label_note), ('correct', 'feedback', 'yes'))
        act = AgentAction.objects.get(action_type='finding_feedback')
        self.assertEqual((act.actor_kind, act.human_confirmed, act.target_model),
                         ('human', True, 'agent.FindingLedger'))

    def test_feedback_next_must_be_local(self):
        row = FindingLedger.objects.get(brand='Nexus')
        self.client.force_login(self.admin)
        r = self.client.post(f'/dashboard/agent/finding/{row.id}/feedback/',
                             {'label': 'unsure', 'next': 'https://evil.example/x'})
        self.assertEqual(r['Location'], '/dashboard/agent/queue/')
        r = self.client.post(f'/dashboard/agent/finding/{row.id}/feedback/',
                             {'label': 'unsure', 'next': '/dashboard/agent/queue/?month=x#findings'})
        self.assertEqual(r['Location'], '/dashboard/agent/queue/?month=x#findings')

    def test_feedback_refused_for_non_admin(self):
        row = FindingLedger.objects.get(brand='Nexus')
        self.client.force_login(f.user(role='team_head', email='th@x.lk', accounts=[self.acc]))
        r = self.client.post(f'/dashboard/agent/finding/{row.id}/feedback/', {'label': 'correct'})
        self.assertEqual(r.status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.label, '')

    def test_queue_lists_findings_with_buttons(self):
        self.client.force_login(self.admin)
        r = self.client.get(f'/dashboard/agent/queue/?month={self.s.month}')
        self.assertContains(r, 'Agent findings')
        self.assertContains(r, 'NO_TC_MAPPING')
        self.assertContains(r, 'value="incorrect"')

    def test_label_csv_requires_exact_schedule_strings(self):
        today = timezone.localdate().isoformat()
        labels, errors = parse(self.csv(['Keells', self.s.channel.lower(), self.s.month, '101',
                                         'NO_TC_MAPPING', 'Nexus', '30', today, '']))
        self.assertEqual(labels, [])
        self.assertIn('strings must match exactly', errors[0])
        _, errors = parse(self.csv(['Keells', self.s.channel, self.s.month, '101', 'MADE_UP', '', '', today, '']))
        self.assertIn('unknown cause_code', errors[0])

    def test_owner_labels_correct_miss_and_no_issue(self):
        today = timezone.localdate().isoformat()
        path = self.csv(['Keells', self.s.channel, self.s.month, '101', 'NO_TC_MAPPING', 'Nexus', '30', today, 'blank'],
                        ['Keells', self.s.channel, self.s.month, '101', 'TC_NO_ROWS', '', '', today, 'missed one'])
        call_command('agent_label_scopes', path, '--actor', self.admin.email)
        nexus = FindingLedger.objects.get(brand='Nexus')
        self.assertEqual((nexus.label, nexus.label_source, nexus.cause_code), ('correct', 'owner', 'NO_TC_MAPPING'))
        self.assertEqual(nexus.label_as_of, datetime.date.fromisoformat(today))
        miss = FindingLedger.objects.get(code='TC_NO_ROWS')
        self.assertEqual((miss.resolution, miss.open), (ledger.OWNER_ONLY, False))
        self.assertEqual(measure.misses(), {'TC_NO_ROWS': 1})
        self.assertEqual(AgentAction.objects.filter(action_type='owner_label', actor_kind='human').count(), 2)
        call_command('agent_label_scopes', self.csv(['Keells', self.s.channel, self.s.month, '101', 'no_issue',
                                                     '', '', today, '']), '--actor', self.admin.email)
        self.assertEqual(FindingLedger.objects.get(brand='Other').label, 'incorrect')

    def test_owner_only_row_found_later_is_not_a_reopen(self):
        today = timezone.localdate().isoformat()
        call_command('agent_label_scopes', self.csv(['Keells', self.s.channel, self.s.month, '101', 'TC_NO_ROWS',
                                                     '', '', today, '']), '--actor', self.admin.email)
        fp = fingerprint(self.sc)
        ledger.observe(self.sc, [unmapped(), unmapped('Other'), Finding('TC_NO_ROWS', '0 rows')], fp, fp,
                       scope_context(self.sc))
        row = FindingLedger.objects.get(code='TC_NO_ROWS')
        self.assertEqual((row.open, row.reopen_count, row.label), (True, 0, 'correct'))

    def test_precision_formula(self):
        FindingLedger.objects.filter(brand='Nexus').update(label='correct', label_source='feedback')
        FindingLedger.objects.filter(brand='Other').update(label='incorrect', label_source='owner')
        p = measure.precision()
        self.assertEqual(p['overall']['precision'], 0.5)
        self.assertEqual(p['overall']['n'], 2)
        self.assertEqual(p['unmapped_brand']['n'], 2)
        self.assertEqual(measure.precision('feedback')['overall']['precision'], 1.0)
        self.assertEqual(measure.inferred()['label'], 'inferred')

    def test_label_command_refuses_whole_file_on_one_bad_row(self):
        today = timezone.localdate().isoformat()
        path = self.csv(['Keells', self.s.channel, self.s.month, '101', 'NO_TC_MAPPING', 'Nexus', '30', today, ''],
                        ['Nobody', self.s.channel, self.s.month, '101', 'NO_TC_MAPPING', '', '', today, ''])
        with self.assertRaises(SystemExit):
            call_command('agent_label_scopes', path, '--actor', self.admin.email)
        self.assertFalse(FindingLedger.objects.filter(label_source='owner').exists())


class LabelledEvalTest(TestCase):
    def setUp(self):
        self.acc, self.s = f.full_scope()

    def test_refuses_without_disposable_flag(self):
        with mock.patch.dict(os.environ, {'AGENT_DISPOSABLE_DB': ''}):
            with self.assertRaises(CommandError):
                call_command('agent_diagnose_labelled', 'x.csv')

    def test_evaluate_counts_tp_fp_miss(self):
        from agent.labels import Label
        d = datetime.date(2025, 2, 3)
        labs = [Label(2, self.acc, self.s, 'NO_TC_MAPPING', 'Nexus', 30, d, ''),
                Label(3, self.acc, self.s, 'TC_NO_ROWS', '', None, d, '')]
        key = (self.acc.id, self.s.channel, self.s.month)
        found = {key: [{'code': 'NO_TC_MAPPING', 'brand': 'Nexus', 'duration': 30},
                       {'code': 'NO_TC_MAPPING', 'brand': 'Other', 'duration': 30},
                       {'code': 'SUPERSEDED_ROWS_PRESENT', 'brand': '', 'duration': None}]}
        res = evaluate(labs, found)
        rows = {r['code']: r for r in res['codes']}
        self.assertEqual((rows['NO_TC_MAPPING']['tp'], rows['NO_TC_MAPPING']['fp']), (1, 1))
        self.assertEqual(rows['TC_NO_ROWS']['miss'], 1)
        self.assertNotIn('SUPERSEDED_ROWS_PRESENT', rows)                 # info only: not scored
        self.assertEqual((res['overall']['precision'], res['overall']['recall']), (0.5, 0.5))

    def test_runs_read_only_and_writes_report(self):
        fd, path = tempfile.mkstemp(suffix='.csv')
        with os.fdopen(fd, 'w') as fh:
            fh.write('account,channel,month,schedule_number,cause_code,brand,duration,as_of,note\n')
            fh.write(f'Keells,{self.s.channel},{self.s.month},101,no_issue,,,2025-02-03,\n')
        out = tempfile.mktemp(suffix='.md')
        self.addCleanup(lambda: os.path.exists(out) and os.remove(out))
        with mock.patch.dict(os.environ, {'AGENT_DISPOSABLE_DB': '1'}):
            call_command('agent_diagnose_labelled', path, '--output', out)
        os.remove(path)
        text = open(out).read()
        self.assertIn('| **Overall** |', text)
        from agent.models import AgentRun
        self.assertTrue(AgentRun.objects.filter(kind='labelled_eval').exists())
        self.assertFalse(ScopeState.objects.exists())                     # nothing saved for the scope
