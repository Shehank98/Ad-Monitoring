"""Phase 3 b/c: observed vs shadow snapshots, the DB guard, S1 parity, S9 authorisation guard."""
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from agent import validate
from agent.canonical import canonical_json, sha256_of
from agent.db import CoreWriteAttempt, Yielded, guard
from agent.effects import diff_summaries
from agent.fingerprint import fingerprint
from agent.models import AgentAuthorisation, AgentConfig, ScopeState, SummarySnapshot
from agent.snapshots import read_observed, store_observed
from core.models import Schedule

from . import factories as f

PG = connection.vendor == 'postgresql'


def scope_of(acc, s):
    return ScopeState.objects.get_or_create(account=acc, channel=s.channel, month=s.month)[0]


class ParityWithSummaryPageTest(TestCase):
    """S1: the observed snapshot equals what the core Summary page renders (summary_data),
    and both the page GET and the observed read run inside READ ONLY transactions on PostgreSQL.
    A write there raises CoreWriteAttempt (stop-and-ask), which fails this test."""

    def test_observed_equals_summary_page(self):
        acc, s = f.full_scope()
        admin = f.user(role='admin')
        self.client.force_login(admin)
        sc = scope_of(acc, s)
        data, fp, active = read_observed(sc)                            # READ ONLY on PostgreSQL
        snaps = store_observed(sc, data, fp, active)
        url = f'/dashboard/summary/?account_id={acc.id}&channel={s.channel}&month={s.month}&schedule_id={s.id}'
        with guard(read_only=True):                                      # the page GET, read only
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        page = resp.context['summary_data']
        from agent.canonical import to_jsonable
        self.assertEqual(canonical_json(to_jsonable(page)), canonical_json(snaps[s.id].data))
        self.assertEqual(snaps[s.id].kind, 'observed')

    @skipUnless(PG, 'READ ONLY enforcement is PostgreSQL-only')
    def test_write_inside_read_only_guard_is_stop_and_ask(self):
        acc, s = f.full_scope()
        with self.assertRaises(CoreWriteAttempt):
            with guard(read_only=True):
                Schedule.objects.filter(pk=s.pk).update(schedule_number='999')
        s.refresh_from_db()
        self.assertEqual(s.schedule_number, '101')


class BaselineAndAuthorisationTest(TestCase):
    def test_shadow_is_never_the_v5_baseline(self):
        acc, s = f.full_scope()
        sc = scope_of(acc, s)
        fp = fingerprint(sc)
        SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101', kind='observed', data={},
                                       sha256='observed-sha', fingerprint=fp,
                                       fingerprint_sha256=sha256_of(fp))
        SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101', kind='shadow', data={},
                                       sha256='shadow-sha')                        # newer, but shadow
        c = validate.v5(sc, s, 'shadow-sha', fp)
        self.assertEqual(c.detail['previous_sha'], 'observed-sha')
        self.assertTrue(c.detail['changed'])

    def test_shadow_or_golden_snapshot_cannot_be_authorised(self):
        acc, s = f.full_scope()
        sc = scope_of(acc, s)
        admin = f.user(role='admin')
        for kind in ('shadow', 'golden'):
            snap = SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101', kind=kind,
                                                  data={}, sha256=kind)
            with self.assertRaises(ValidationError):
                AgentAuthorisation.objects.create(schedule=s, snapshot=snap, snapshot_sha256=kind,
                                                  authorised_by=admin)
        ok = SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number='101', kind='observed',
                                            data={}, sha256='o')
        AgentAuthorisation.objects.create(schedule=s, snapshot=ok, snapshot_sha256='o', authorised_by=admin)


class EffectsTest(TestCase):
    def test_diff_per_brand_and_totals(self):
        obs = {'commercial': [{'product': 'Nexus', 'dur': 30, 'planned': 4, 'aired': 1, 'third_party': 1,
                               'extra': 0, 'missed': 3}],
               'commercial_total': {'planned': 4, 'aired': 1, 'third_party': 1, 'extra': 0, 'missed': 3},
               'sponsorship': [], 'sponsorship_total': {}}
        sh = {'commercial': [{'product': 'Nexus', 'dur': 30, 'planned': 4, 'aired': 3, 'third_party': 3,
                              'extra': 0, 'missed': 1},
                             {'product': 'Krest', 'dur': 20, 'planned': 0, 'aired': 1, 'third_party': 1,
                              'extra': 1, 'missed': 0}],
              'commercial_total': {'planned': 4, 'aired': 4, 'third_party': 4, 'extra': 1, 'missed': 1},
              'sponsorship': [{'programme': 'News', 'rows': [{'product': 'Soda', 'dur': 10, 'aired': 2}]}],
              'sponsorship_total': {}}
        d = diff_summaries(obs, sh)
        nexus = next(x for x in d['by_brand'] if x['product'] == 'Nexus')
        self.assertEqual(nexus['deltas'], {'aired': 2, 'third_party': 2, 'missed': -2})
        self.assertEqual(d['max_abs'], 2)
        self.assertEqual(d['totals']['commercial_total']['aired'], 3)
        self.assertTrue(any(x['section'] == 'sponsorship' and x['programme'] == 'News' for x in d['by_brand']))
        self.assertEqual(diff_summaries(obs, obs), {'by_brand': [], 'totals': {}, 'max_abs': 0})


@skipUnless(PG, 'SET LOCAL timeouts are PostgreSQL-only')
class GuardTimeoutsTest(TransactionTestCase):
    def setUp(self):
        c = AgentConfig.get_solo()
        c.db_lock_timeout_ms, c.db_statement_timeout_ms, c.db_idle_timeout_ms = 300, 500, 60000
        c.save()

    def test_set_local_values_applied(self):
        with guard():
            with connection.cursor() as cur:
                cur.execute('SHOW lock_timeout')
                self.assertEqual(cur.fetchone()[0], '300ms')
                cur.execute('SHOW statement_timeout')
                self.assertEqual(cur.fetchone()[0], '500ms')
                cur.execute('SHOW idle_in_transaction_session_timeout')
                self.assertEqual(cur.fetchone()[0], '1min')
        with connection.cursor() as cur:                                 # LOCAL: gone after the block
            cur.execute('SHOW lock_timeout')
            self.assertEqual(cur.fetchone()[0], '0')

    def test_lock_timeout_yields_busy(self):
        acc, s = f.full_scope()
        other = connections.create_connection('default')
        other.set_autocommit(False)
        with other.cursor() as cur:
            cur.execute('SELECT id FROM core_schedule WHERE id = %s FOR UPDATE', [s.id])
        try:
            with self.assertRaises(Yielded) as cm:
                with guard():
                    list(Schedule.objects.select_for_update().filter(pk=s.pk))
            self.assertEqual(cm.exception.reason, 'busy_yielded')
        finally:
            other.rollback()
            other.close()

    def test_statement_timeout_yields(self):
        with self.assertRaises(Yielded) as cm:
            with guard():
                with connection.cursor() as cur:
                    cur.execute('SELECT pg_sleep(2)')
        self.assertEqual(cm.exception.reason, 'timeout_yielded')
