from io import StringIO

from django.core.management import call_command
from django.test import RequestFactory, TestCase

from agent.models import AgentAction, Heartbeat
from agent.service import ServiceUserError, ensure_service_user, sync_service_user
from core.views import summary_excel

from . import factories as f


class ServiceUserTest(TestCase):
    def test_created_least_privileged_with_all_accounts(self):
        a, b = f.account('A'), f.account('B')
        user, created, added = ensure_service_user()
        self.assertTrue(created)
        self.assertEqual(user.role, 'operations')
        self.assertFalse(user.has_usable_password())
        self.assertEqual(set(user.accounts.values_list('id', flat=True)), {a.id, b.id})

    def test_resync_adds_new_accounts(self):
        f.account('A')
        ensure_service_user()
        c = f.account('C')
        user, created, changes = ensure_service_user()
        self.assertFalse(created)
        self.assertEqual(changes, [f'accounts added: [{c.id}]'])
        self.assertIn(c.id, user.accounts.values_list('id', flat=True))

    def test_can_export_summary_in_process(self):
        """A12: stop and ask if the service user cannot export the Summary in-process."""
        acc, s = f.full_scope()
        user, *_ = ensure_service_user()
        req = RequestFactory().get('/dashboard/summary/excel/', {
            'account_id': acc.id, 'channel': s.channel, 'month': s.month, 'schedule_id': s.id})
        req.user = user
        req._messages = type('M', (), {'add': lambda *a, **k: None})()
        resp = summary_excel(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheet', resp['Content-Type'])


class ServiceUserSyncTest(TestCase):
    """Owner decision 6 / guardian check 18."""

    def test_command_prints_every_change_and_logs_no_action(self):
        f.account('A')
        out = StringIO()
        call_command('agent_ensure_service_user', stdout=out)
        text = out.getvalue()
        self.assertIn('changed: created user', text)
        self.assertIn('changed: accounts added', text)
        self.assertFalse(AgentAction.objects.exists())

    def test_cycle_sync_adds_only_and_records_action(self):
        a = f.account('A')
        user, *_ = ensure_service_user()
        other = f.user(role='planner', accounts=[a])
        b = f.account('B')
        pw, active = user.password, user.is_active
        user2, action = sync_service_user()
        self.assertEqual(action.action_type, 'service_user_account_sync')
        self.assertEqual(action.before, {'account_ids': [a.id]})
        self.assertEqual(action.after, {'account_ids': [a.id, b.id]})
        self.assertEqual(action.actor_id, user.id)
        user.refresh_from_db()
        self.assertEqual((user.role, user.password, user.is_active), ('operations', pw, active))
        self.assertEqual(list(other.accounts.values_list('id', flat=True)), [a.id])   # other user untouched
        self.assertIsNone(sync_service_user()[1])                                       # nothing to add

    def test_cycle_sync_never_removes_accounts(self):
        a = f.account('A')
        user, *_ = ensure_service_user()
        extra = f.account('Z')
        user.accounts.add(extra)
        sync_service_user()
        self.assertEqual(set(user.accounts.values_list('id', flat=True)), {a.id, extra.id})

    def test_wrong_role_stops_cycle_logs_and_alerts(self):
        f.account('A')
        user, *_ = ensure_service_user()
        type(user).objects.filter(pk=user.pk).update(role='admin')
        with self.assertLogs('agent', level='ERROR'), self.assertRaises(ServiceUserError):
            sync_service_user()
        hb = Heartbeat.objects.get(name='agent_cycle')
        self.assertTrue(hb.alert)
        self.assertIn("'admin'", hb.alert_message)
        user.refresh_from_db()
        self.assertEqual(user.role, 'admin')                  # the cycle never fixes the role itself
        self.assertFalse(AgentAction.objects.exists())

    def test_missing_user_stops_cycle(self):
        with self.assertLogs('agent', level='ERROR'), self.assertRaises(ServiceUserError):
            sync_service_user()
        self.assertTrue(Heartbeat.objects.get(name='agent_cycle').alert)
