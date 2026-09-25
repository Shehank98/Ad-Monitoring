"""Mail fetch: idempotency (C7), file types (C8), size (C), unknown sender, lock (C9), gate (Q6)."""
from unittest import mock

from django.test import TestCase, TransactionTestCase

from agent import gate
from agent.models import AgentAction, AgentConfig
from agent.service import ensure_service_user
from agent.tests import factories as f
from intake.cron import fetch as F
from intake.cron.lock import intake_lock
from intake.mailbox import FakeMailbox
from intake.models import AllowedSender, InboundAttachment, InboundEmail

from .fakes import raw


def setup_fetch(enabled=True, sender='tv.lk'):
    c = AgentConfig.get_solo()
    c.intake_fetch_enabled = enabled
    c.save()
    ensure_service_user()
    if sender:
        AllowedSender.objects.create(email_or_domain=sender)


class FetchTest(TestCase):
    def test_same_email_twice_is_stored_once(self):
        setup_fetch()
        mb = FakeMailbox([raw()])
        self.assertEqual(F.fetch_emails(mb)['stored'], 1)
        self.assertEqual(F.fetch_emails(mb)['stored'], 0)
        self.assertEqual(InboundEmail.objects.count(), 1)
        att = InboundAttachment.objects.get()
        self.assertEqual((att.status, att.content.tobytes() if hasattr(att.content, 'tobytes') else bytes(att.content)),
                         ('new', b'PK-fake'))
        acts = AgentAction.objects.filter(action_type='intake_fetch')
        self.assertEqual(acts.count(), 2)
        self.assertTrue(all(a.actor_kind == 'intake_fetch' for a in acts))

    def test_same_attachment_on_another_email_is_duplicate(self):
        setup_fetch()
        F.fetch_emails(FakeMailbox([raw(), raw(message_id='<m2@tv.lk>', subject='FW: TC')]))
        a1, a2 = InboundAttachment.objects.order_by('id')
        self.assertEqual((a2.status, a2.reason, a2.duplicate_of_id), ('duplicate', 'duplicate_attachment', a1.id))
        self.assertEqual(bytes(a2.content), b'')

    def test_unsupported_types_ignored(self):
        setup_fetch()
        F.fetch_emails(FakeMailbox([raw(attachments=(('tc.docx', b'1'), ('tc.csv', b'2'), ('tc.PDF', b'3')))]))
        got = {a.filename: (a.status, a.reason) for a in InboundAttachment.objects.all()}
        self.assertEqual(got['tc.docx'], ('ignored', 'unsupported_type'))
        self.assertEqual(got['tc.csv'], ('ignored', 'unsupported_type'))
        self.assertEqual(got['tc.PDF'], ('new', ''))

    def test_too_large_stores_no_content(self):
        setup_fetch()
        with mock.patch.object(F, 'MAX_ATTACHMENT_BYTES', 4):
            F.fetch_emails(FakeMailbox([raw(attachments=(('tc.xlsx', b'12345'),))]))
        a = InboundAttachment.objects.get()
        self.assertEqual((a.status, a.reason, bytes(a.content), a.size), ('needs_review', 'too_large', b'', 5))

    def test_unknown_sender(self):
        setup_fetch(sender='radio.lk')
        F.fetch_emails(FakeMailbox([raw(sender='someone@tv.lk')]))
        e = InboundEmail.objects.get()
        self.assertEqual((e.status, e.reason), ('needs_review', 'unknown_sender'))
        self.assertEqual(e.attachments.get().reason, 'unknown_sender')

    def test_fetch_ignores_kill_switch_but_needs_flag_and_sender(self):
        setup_fetch(enabled=False)
        mb = FakeMailbox([raw()])
        with self.assertRaises(gate.FetchDisabled):
            F.fetch_emails(mb)
        self.assertEqual(mb.calls, 0)                        # mailbox not even read
        c = AgentConfig.get_solo()
        c.enabled, c.intake_fetch_enabled = False, True       # agent OFF, fetch ON
        c.save()
        self.assertEqual(F.fetch_emails(mb)['status'], 'ok')
        AllowedSender.objects.update(active=False)
        with self.assertRaises(gate.FetchDisabled):
            F.fetch_emails(FakeMailbox([raw(message_id='<x@tv.lk>')]))

    def test_no_attachments(self):
        setup_fetch()
        F.fetch_emails(FakeMailbox([raw(attachments=())]))
        self.assertEqual(InboundEmail.objects.get().reason, 'no_tc_attachment')


class IntakeLockTest(TransactionTestCase):
    def test_overlapping_run_exits_at_once(self):
        setup_fetch()
        mb = FakeMailbox([raw()])
        with intake_lock() as got:
            self.assertTrue(got)
            if __import__('agent.locks', fromlist=['is_postgres']).is_postgres():
                # PostgreSQL session locks are re-entrant within one connection; use a
                # second connection to prove exclusion.
                from django.db import connections
                other = connections.create_connection('default')
                with other.cursor() as cur:
                    cur.execute('SELECT pg_try_advisory_lock(%s)', [__import__('intake.cron.lock', fromlist=['x']).INTAKE_LOCK_KEY])
                    self.assertFalse(cur.fetchone()[0])
                other.close()
            else:
                self.assertEqual(F.fetch_emails(mb), {'status': 'busy'})
                self.assertEqual(mb.calls, 0)
        self.assertEqual(F.fetch_emails(mb)['status'], 'ok')
