"""Owner Q1: read-only mailboxes, each with a fake transport."""
from datetime import timedelta

from django.test import SimpleTestCase

from intake.mailbox import GRAPH_BASE, GraphMailbox, ImapGuard, ImapMailbox, ReadOnlyViolation

from .fakes import WHEN, FakeGraphTransport, FakeImapConn, b64, rfc822

WRITE_COMMANDS = {'STORE', 'EXPUNGE', 'MOVE', 'COPY', 'APPEND', 'SELECT', 'DELETE'}


class ImapTest(SimpleTestCase):
    def test_fetch_uses_examine_and_peek_only(self):
        conn = FakeImapConn({'7': rfc822(attachments=(('tc.xlsx', b'abc'),))})
        msgs = ImapMailbox(conn, 'tc@agency.lk', 'pw', folder='TC').fetch(WHEN - timedelta(days=1))
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        self.assertEqual((m.message_id, m.sender, m.uid), ('<m1@tv.lk>', 'desk@tv.lk', '7'))
        self.assertEqual(m.attachments[0].filename, 'tc.xlsx')
        self.assertEqual(m.attachments[0].data, b'abc')
        sent = {c[0] if c[0] != 'UID' else c[1] for c in conn.commands}
        self.assertFalse(sent & WRITE_COMMANDS, conn.commands)
        self.assertIn(('EXAMINE', 'TC'), conn.commands)
        fetches = [c for c in conn.commands if c[:2] == ('UID', 'FETCH')]
        self.assertTrue(all('BODY.PEEK[]' in c[3] for c in fetches))

    def test_xoauth2_login(self):
        conn = FakeImapConn({})
        ImapMailbox(conn, 'tc@agency.lk', 'tok', auth='xoauth2').fetch(WHEN)
        auth = [c for c in conn.commands if c[0] == 'AUTHENTICATE'][0]
        self.assertEqual(auth[1], 'XOAUTH2')
        self.assertEqual(auth[2], b'user=tc@agency.lk\x01auth=Bearer tok\x01\x01')
        self.assertNotIn('LOGIN', [c[0] for c in conn.commands])

    def test_guard_refuses_every_write(self):
        g = ImapGuard(FakeImapConn({}))
        for cmd in ('STORE', 'COPY', 'MOVE', 'EXPUNGE'):
            with self.assertRaises(ReadOnlyViolation):
                g.uid(cmd, '1', '+FLAGS', '\\Deleted')
        with self.assertRaises(ReadOnlyViolation):
            g.uid('FETCH', '1', '(RFC822)')                  # would set \\Seen
        for name in ('store', 'expunge', 'select', 'append', 'delete'):
            with self.assertRaises(ReadOnlyViolation):
                getattr(g, name)


class GraphTest(SimpleTestCase):
    def setUp(self):
        self.prefix = f'{GRAPH_BASE}/users/tc@agency.lk/'
        self.t = FakeGraphTransport({
            f'{self.prefix}mailFolders/inbox/messages': {'value': [{
                'id': 'AAA', 'internetMessageId': '<g1@tv.lk>', 'subject': 'TC',
                'from': {'emailAddress': {'address': 'Desk@TV.lk'}},
                'receivedDateTime': '2025-02-03T09:00:00Z', 'body': {'content': 'hi'},
                'hasAttachments': True}]},
            f'{self.prefix}messages/AAA/attachments': {'value': [{
                '@odata.type': '#microsoft.graph.fileAttachment', 'name': 'tc.pdf',
                'contentType': 'application/pdf', 'contentBytes': b64(b'%PDF')}]},
        })

    def test_get_only_on_the_one_mailbox(self):
        msgs = GraphMailbox(self.t, 'TC@agency.lk').fetch(WHEN - timedelta(days=1))
        self.assertEqual(msgs[0].message_id, '<g1@tv.lk>')
        self.assertEqual(msgs[0].sender, 'desk@tv.lk')
        self.assertEqual(msgs[0].attachments[0].data, b'%PDF')
        self.assertTrue(all(m == 'GET' and u.startswith(self.prefix) for m, u in self.t.calls))

    def test_refuses_other_mailbox_via_next_link(self):
        page = self.t.pages[f'{self.prefix}mailFolders/inbox/messages']
        page['@odata.nextLink'] = f'{GRAPH_BASE}/users/ceo@agency.lk/messages'
        with self.assertRaises(ReadOnlyViolation):
            GraphMailbox(self.t, 'tc@agency.lk').fetch(WHEN)
        self.assertFalse([u for _, u in self.t.calls if 'ceo@' in u])

    def test_transport_has_no_write_method(self):
        from intake.mailbox import RequestsGraphTransport
        self.assertFalse(any(hasattr(RequestsGraphTransport, m) for m in ('post_mail', 'patch', 'delete', 'put')))
