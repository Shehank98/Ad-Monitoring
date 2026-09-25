import ast
import pathlib

from django.test import TestCase

from agent import validate
from agent.canonical import sha256_of
from agent.fingerprint import fingerprint, fingerprint_sha
from agent.models import AgentAction, ScopeState, SummarySnapshot

from . import factories as f


def summary(**row):
    base = {'product': 'Nexus', 'dur': 30, 'planned': 10, 'aired': 8, 'third_party': 8, 'extra': 0, 'missed': 2}
    base.update(row)
    return {'commercial': [base], 'sponsorship': []}


class V1Test(TestCase):
    def setUp(self):
        self.acc = f.account()

    def test_mapped_consistent(self):
        f.mapping(self.acc)
        self.assertTrue(validate.v1(summary(), self.acc.id))

    def test_mapped_aired_includes_manual(self):
        f.mapping(self.acc)
        self.assertTrue(validate.v1(summary(aired=9), self.acc.id))   # aired = 3rd party + manual

    def test_mapped_inconsistent_is_validation_fail(self):
        f.mapping(self.acc)
        rows = validate.v1_rows(summary(missed=1), self.acc.id)
        self.assertEqual(rows[0].code, 'VALIDATION_FAIL')
        rows = validate.v1_rows(summary(aired=7), self.acc.id)      # aired < third_party
        self.assertEqual(rows[0].code, 'VALIDATION_FAIL')

    def test_unmapped_rows(self):
        rows = validate.v1_rows(summary(aired=2, third_party=0, missed=8, extra=0), self.acc.id)
        self.assertEqual((rows[0].code, rows[0].ok), ('NO_TC_MAPPING', True))
        rows = validate.v1_rows(summary(third_party=3, missed=7), self.acc.id)
        self.assertEqual(rows[0].code, 'VALIDATION_FAIL')

    def test_no_third_party_ge_aired_check_anywhere(self):
        """A3: the check '3rd Party >= Aired' must not exist in agent code."""
        root = pathlib.Path(__file__).resolve().parents[1]
        for p in root.rglob('*.py'):
            if 'tests' in p.parts:
                continue
            src = p.read_text()
            for pat in ("third_party >= aired", "third_party>=aired", "aired <= third_party"):
                self.assertNotIn(pat, src.replace(' ', ' '), f'{p}: forbidden check')


class V2V3V5Test(TestCase):
    def test_v2(self):
        self.assertTrue(validate.v2(3, 3).ok)
        self.assertFalse(validate.v2(3, 4).ok)
        self.assertTrue(validate.v2(3, 2).ok)

    def test_v3_byte_equal(self):
        acc = f.account()
        s = f.schedule(acc)
        f.tc_report(acc, s)
        self.assertTrue(validate.v3([s]).ok)
        f.tc_report(acc, s, channel='Sirasa TV ')
        self.assertFalse(validate.v3([s]).ok)

    def test_v5(self):
        acc = f.account()
        s = f.schedule(acc)
        sc = ScopeState.objects.create(account=acc, channel=f.CHANNEL, month=f.MONTH)
        fp = fingerprint(sc)
        self.assertTrue(validate.v5(sc, s, 'x', fp).ok)                 # no snapshot yet
        SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101', data={},
                                       sha256=sha256_of({}), fingerprint=fp,
                                       fingerprint_sha256=fingerprint_sha(fp))
        self.assertTrue(validate.v5(sc, s, sha256_of({}), fp).ok)       # unchanged
        self.assertFalse(validate.v5(sc, s, 'changed', fp).ok)          # unexplained
        AgentAction.objects.create(action_type='x', scope=sc)
        c = validate.v5(sc, s, 'changed', fp)
        self.assertTrue(c.ok)                                            # explained
        self.assertEqual(c.detail['explained_by'], ['agent_action'])
