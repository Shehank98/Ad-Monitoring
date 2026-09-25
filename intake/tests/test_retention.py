"""Owner C6 (retention) and Q2 (sender import)."""
import csv
import tempfile
from datetime import timedelta
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from agent.models import AgentAction, AgentConfig
from agent.service import ensure_service_user
from agent.tests import factories as f
from intake.management.commands.intake_purge_content import purge
from intake.models import AllowedSender, InboundAttachment, InboundEmail


class PurgeTest(TestCase):
    def test_clears_content_keeps_evidence_one_action(self):
        ensure_service_user()
        c = AgentConfig.get_solo()
        c.enabled = c.intake_fetch_enabled = False               # runs even with everything off
        c.save()
        old = InboundEmail.objects.create(message_id='<o>', sender='d@tv.lk', body_text='body')
        InboundEmail.objects.filter(pk=old.pk).update(fetched_at=timezone.now() - timedelta(days=100))
        done = InboundAttachment.objects.create(email=old, filename='a.xlsx', sha256='a' * 64, content=b'xx',
                                                status='uploaded', evidence={'overlap': 0.9})
        pending = InboundAttachment.objects.create(email=old, filename='b.xlsx', sha256='b' * 64,
                                                   content=b'yy', status='needs_review')
        fresh = InboundEmail.objects.create(message_id='<n>', sender='d@tv.lk', body_text='new')
        InboundAttachment.objects.create(email=fresh, filename='c.xlsx', sha256='c' * 64, content=b'zz',
                                         status='uploaded')
        res = purge(90)
        done.refresh_from_db(); pending.refresh_from_db(); old.refresh_from_db()
        self.assertEqual(bytes(done.content), b'')
        self.assertEqual((done.sha256, done.evidence, done.status), ('a' * 64, {'overlap': 0.9}, 'uploaded'))
        self.assertIsNotNone(done.purged_at)
        self.assertEqual(bytes(pending.content), b'yy')          # still waiting for a person
        self.assertEqual(old.body_text, 'body')                   # an item of this email is still open
        self.assertEqual(res['attachments_purged'], 1)
        act = AgentAction.objects.get(action_type='intake_purge_content')
        self.assertEqual(act.actor_kind, 'intake_retention')


class ImportSendersTest(TestCase):
    def _csv(self, rows):
        fh = tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, newline='')
        w = csv.DictWriter(fh, fieldnames=['email_or_domain', 'channel_hint', 'accounts', 'note'])
        w.writeheader()
        w.writerows(rows)
        fh.close()
        return fh.name

    def test_import_logs_actions_and_sets_accounts(self):
        admin = f.user(role='admin')
        a, b = f.account('Keells'), f.account('Dialog')
        path = self._csv([{'email_or_domain': 'TV.lk', 'channel_hint': 'Sirasa TV', 'accounts': '', 'note': ''},
                          {'email_or_domain': 'desk@radio.lk', 'channel_hint': '', 'accounts': 'Keells;Dialog',
                           'note': 'traffic'}])
        out = StringIO()
        call_command('intake_import_senders', path, '--actor', admin.email, stdout=out)
        self.assertEqual(AllowedSender.objects.count(), 2)
        self.assertEqual(AllowedSender.objects.get(email_or_domain='tv.lk').accounts.count(), 0)   # = all
        self.assertEqual(set(AllowedSender.objects.get(email_or_domain='desk@radio.lk')
                             .accounts.values_list('id', flat=True)), {a.id, b.id})
        acts = AgentAction.objects.filter(action_type='allowed_sender_import')
        self.assertEqual(acts.count(), 2)
        self.assertTrue(all(x.human_confirmed and x.actor_id == admin.id for x in acts))

    def test_non_admin_refused_and_unknown_account_refused(self):
        planner = f.user(role='planner', email='p@t.com')
        path = self._csv([{'email_or_domain': 'tv.lk', 'channel_hint': '', 'accounts': '', 'note': ''}])
        with self.assertRaises(CommandError):
            call_command('intake_import_senders', path, '--actor', planner.email, stdout=StringIO())
        admin = f.user(role='admin')
        bad = self._csv([{'email_or_domain': 'x.lk', 'channel_hint': '', 'accounts': 'Nope', 'note': ''}])
        with self.assertRaises(CommandError):
            call_command('intake_import_senders', bad, '--actor', admin.email, stdout=StringIO())
        self.assertFalse(AllowedSender.objects.exists())


class PurgeBoundsTest(TestCase):
    def test_days_lower_bound(self):
        with self.assertRaises(ValueError):
            purge(0)
