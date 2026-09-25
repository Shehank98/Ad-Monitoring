from django.test import RequestFactory, TestCase

from agent.service import ensure_service_user
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
        user, created, added = ensure_service_user()
        self.assertFalse(created)
        self.assertEqual(added, 1)
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
