"""Tests for the read-only Reconciliation Agent preview pages (/dashboard/agent/)."""
import datetime

from django.test import TestCase

from accounts.models import User
from core.models import (
    Account, BrandMapping, LMRBRow, Schedule, ScheduleRow, SummaryReportMeta,
    TCRow, TransmissionReport,
)

from agent.scopes import build_scope, build_scopes

CHANNEL = 'Sirasa TV'
MONTH = 'January 2025'
TODAY = datetime.date(2025, 3, 1)   # period (January) has ended


def _account(name='Keells'):
    return Account.objects.create(name=name)


def _user(role='admin', email=None, accounts=()):
    u = User.objects.create_user(email=email or f'{role}@t.com', name=f'{role} user',
                                 password='password123', role=role, must_change_password=False)
    if accounts:
        u.accounts.set(accounts)
    return u


def _schedule(acc, number='101', version=1, channel=CHANNEL, month=MONTH):
    return Schedule.objects.create(
        account=acc, channel=channel, month=month, schedule_number=number, version=version,
        file='schedules/x.xlsx', original_filename='x.xlsx',
        start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 1, 31))


def _row(acc, sched, brand='Nexus', dur=30, day=10):
    return ScheduleRow.objects.create(
        schedule=sched, account=acc, channel=sched.channel, month=sched.month, brand=brand,
        programme='News', date=datetime.date(2025, 1, day), start_time='20:00:00',
        end_time='20:15:00', duration=dur, ad_type='COMMERCIAL BENEFITS')


def _lmrb(acc, day=31, channel=CHANNEL):
    d = datetime.date(2025, 1, day)
    return LMRBRow.objects.create(
        account=acc, channel=channel, date=d, advt_theme='Nexus (30)(Sin)', advt_time='20:05:00',
        duration=30, source='mediawatch',
        dedup_key=LMRBRow.make_dedup_key(acc.id, channel, d, '20:05:00', 'Nexus (30)(Sin)', 30))


def _tc_report(acc, sched=None, channel=CHANNEL):
    return TransmissionReport.objects.create(
        account=acc, channel=channel, month=MONTH, schedule=sched,
        file='tc/x.xlsx', original_filename='x.xlsx')


def _map(acc, brand='Nexus', tc='NEXUS 30'):
    return BrandMapping.objects.create(account=acc, brand=brand, theme='Nexus (30)(Sin)', tc_theme=tc)


class ScopeStateTest(TestCase):
    def setUp(self):
        self.acc = _account()
        self.s = _schedule(self.acc)
        _row(self.acc, self.s)

    def state(self):
        return build_scope(self.acc.id, self.acc.name, CHANNEL, MONTH, today=TODAY)

    def test_still_airing_before_period_end(self):
        sc = build_scope(self.acc.id, self.acc.name, CHANNEL, MONTH, today=datetime.date(2025, 1, 20))
        self.assertEqual(sc.state, 'STILL_AIRING')

    def test_no_lmrb(self):
        sc = self.state()
        self.assertEqual((sc.state, sc.reason), ('WAITING_INPUTS', 'no_lmrb'))

    def test_partial_lmrb(self):
        _lmrb(self.acc, day=20)
        self.assertEqual(self.state().reason, 'lmrb_partial')

    def test_no_tc_then_unlinked_tc(self):
        _lmrb(self.acc)
        self.assertEqual(self.state().reason, 'no_tc')
        _tc_report(self.acc, sched=None)
        self.assertEqual(self.state().reason, 'tc_not_linked')

    def test_unmapped_brand_is_mapping_state_never_credit(self):
        _lmrb(self.acc)
        _tc_report(self.acc, self.s)
        sc = self.state()
        self.assertEqual(sc.state, 'MAPPING')
        self.assertEqual(sc.unmapped, [('Nexus', 30)])

    def test_ready_when_inputs_and_mapping_complete(self):
        _lmrb(self.acc)
        _tc_report(self.acc, self.s)
        _map(self.acc)
        self.assertEqual(self.state().state, 'READY_FOR_SIGNOFF')

    def test_not_reconciled_when_tc_rows_untouched(self):
        _lmrb(self.acc)
        rep = _tc_report(self.acc, self.s)
        _map(self.acc)
        TCRow.objects.create(account=self.acc, tc_report=rep, channel=CHANNEL,
                             date=datetime.date(2025, 1, 10), tc_theme='NEXUS 30', duration=30,
                             aired_time='20:05:00', dedup_key='k1')
        self.assertEqual(self.state().state, 'RECONCILING')

    def test_wildcard_only_mapping_is_flagged(self):
        _lmrb(self.acc)
        _tc_report(self.acc, self.s)
        _map(self.acc, tc='NEXUS*')
        codes = [f.code for f in self.state().findings]
        self.assertIn('wildcard_only', codes)

    def test_authorised_is_frozen(self):
        SummaryReportMeta.objects.create(account=self.acc, channel=CHANNEL, month=MONTH, authorised_by='K. Fernando')
        sc = self.state()
        self.assertEqual((sc.state, sc.authorised_by), ('AUTHORISED', 'K. Fernando'))

    def test_only_highest_version_is_active(self):
        s2 = _schedule(self.acc, number='101', version=2)
        _row(self.acc, s2, day=11)
        _row(self.acc, s2, day=12)
        sc = self.state()
        self.assertEqual([st.schedule.id for st in sc.schedules], [s2.id])
        self.assertEqual(sc.planned, 2)
        self.assertIn('superseded_present', [f.code for f in sc.findings])

    def test_channel_and_month_are_exact_strings(self):
        sc = build_scopes([self.acc.id], MONTH, today=TODAY)[0]
        self.assertEqual(sc.channel.encode(), self.s.channel.encode())
        self.assertEqual(sc.month.encode(), self.s.month.encode())


class AgentPagesTest(TestCase):
    def setUp(self):
        self.acc = _account('Keells')
        self.other = _account('Dialog')
        for a in (self.acc, self.other):
            s = _schedule(a)
            _row(a, s)
        self.scope_url = f'/dashboard/agent/scope/?account_id={self.acc.id}&channel=Sirasa%20TV&month=January%202025'

    def _counts(self):
        return [m.objects.count() for m in (Schedule, ScheduleRow, LMRBRow, TCRow, BrandMapping, TransmissionReport, SummaryReportMeta)]

    def test_admin_sees_every_page_and_nothing_is_written(self):
        self.client.force_login(_user('admin'))
        before = self._counts()
        for url in ('/dashboard/agent/', '/dashboard/agent/scopes/', self.scope_url,
                    '/dashboard/agent/queue/', '/dashboard/agent/activity/', '/dashboard/agent/config/'):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
        self.assertEqual(before, self._counts())

    def test_channel_officer_is_refused(self):
        self.client.force_login(_user('channel_officer'))
        self.assertEqual(self.client.get('/dashboard/agent/').status_code, 403)

    def test_non_admin_only_sees_assigned_clients(self):
        self.client.force_login(_user('operations', accounts=[self.acc]))
        r = self.client.get('/dashboard/agent/scopes/')
        self.assertContains(r, 'Keells')
        self.assertNotContains(r, 'Dialog')
        other_url = f'/dashboard/agent/scope/?account_id={self.other.id}&channel=Sirasa%20TV&month=January%202025'
        self.assertEqual(self.client.get(other_url).status_code, 403)

    def test_queue_and_settings_are_role_limited(self):
        self.client.force_login(_user('planner', accounts=[self.acc]))
        self.assertEqual(self.client.get('/dashboard/agent/queue/').status_code, 403)
        self.assertEqual(self.client.get('/dashboard/agent/config/').status_code, 403)

    def test_unmapped_brand_appears_in_queue(self):
        _lmrb(self.acc)
        _tc_report(self.acc, Schedule.objects.get(account=self.acc))
        self.client.force_login(_user('admin'))
        r = self.client.get('/dashboard/agent/queue/')
        self.assertContains(r, 'has no TC theme mapping')
