"""
Read-only mailbox clients (owner Q1). The agent never deletes, moves, flags or marks
mail as read.

  Mailbox (protocol)  fetch(since) -> list[RawMessage]
  ImapMailbox         EXAMINE (read-only) + UID SEARCH SINCE + UID FETCH BODY.PEEK[] only.
                      Password or XOAUTH2 login. Every other command is refused by a guard.
  GraphMailbox        GET only, and only on the single mailbox named in
                      INTAKE_GRAPH_MAILBOX (Mail.Read application permission).
  FakeMailbox         for tests.

INTAKE_MAILBOX_TYPE (imap | graph) picks one in get_mailbox().
"""
from __future__ import annotations

import base64
import email
import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from typing import Protocol
from urllib.parse import quote

from django.utils import timezone


@dataclass
class RawAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class RawMessage:
    message_id: str
    sender: str
    subject: str
    received_at: datetime | None
    body_text: str
    attachments: list[RawAttachment] = field(default_factory=list)
    uid: str = ''


class Mailbox(Protocol):
    def fetch(self, since: datetime) -> list[RawMessage]: ...


class ReadOnlyViolation(RuntimeError):
    """A mailbox call would change mail. Never allowed."""


def _synthetic_id(*parts) -> str:
    return '<no-message-id:' + hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:32] + '>'


def parse_rfc822(raw: bytes, uid: str = '') -> RawMessage:
    msg = email.message_from_bytes(raw, policy=policy.default)
    sender = parseaddr(str(msg.get('From', '')))[1].lower()
    subject = str(msg.get('Subject', '') or '')
    try:
        received = parsedate_to_datetime(str(msg.get('Date'))) if msg.get('Date') else None
    except (TypeError, ValueError):
        received = None
    body = msg.get_body(preferencelist=('plain', 'html'))
    body_text = body.get_content() if body is not None else ''
    atts = []
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b''
        atts.append(RawAttachment(part.get_filename() or 'attachment', part.get_content_type(), data))
    mid = str(msg.get('Message-ID', '') or '').strip() or _synthetic_id(sender, subject, received, uid)
    return RawMessage(mid, sender, subject, received, body_text, atts, uid)


# ── IMAP ──────────────────────────────────────────────────────────────────────

class ImapGuard:
    """The only IMAP operations the agent can perform. Wraps an imaplib-like connection
    (real imaplib.IMAP4_SSL in production, a fake in tests)."""
    ALLOWED_UID = ('SEARCH', 'FETCH')

    def __init__(self, conn):
        self._c = conn

    def login(self, user, password):
        return self._c.login(user, password)

    def xoauth2(self, user, token):
        auth = f'user={user}\x01auth=Bearer {token}\x01\x01'.encode()
        return self._c.authenticate('XOAUTH2', lambda _: auth)

    def examine(self, folder):
        return self._c.select(folder, readonly=True)          # imaplib sends EXAMINE

    def uid(self, command, *args):
        cmd = command.upper()
        if cmd not in self.ALLOWED_UID:
            raise ReadOnlyViolation(f'IMAP UID {cmd} is not allowed')
        if cmd == 'FETCH' and 'BODY.PEEK[' not in ' '.join(map(str, args)).upper():
            raise ReadOnlyViolation('IMAP FETCH must use BODY.PEEK[] (never sets \\Seen)')
        return self._c.uid(command, *args)

    def logout(self):
        try:
            return self._c.logout()
        except Exception:      # noqa: BLE001 — closing a read-only session must never fail the run
            return None

    def __getattr__(self, name):
        raise ReadOnlyViolation(f'IMAP operation {name!r} is not allowed')


class ImapMailbox:
    def __init__(self, conn, user: str, secret: str, auth: str = 'password', folder: str = 'INBOX'):
        self.imap = ImapGuard(conn)
        self.user, self.secret, self.auth, self.folder = user, secret, auth, folder

    def fetch(self, since: datetime) -> list[RawMessage]:
        if self.auth == 'xoauth2':
            self.imap.xoauth2(self.user, self.secret)
        else:
            self.imap.login(self.user, self.secret)
        try:
            self.imap.examine(self.folder)
            typ, data = self.imap.uid('SEARCH', None, f'SINCE {since:%d-%b-%Y}')
            uids = (data[0] or b'').split() if data else []
            out = []
            for u in uids:
                uid = u.decode() if isinstance(u, bytes) else str(u)
                typ, parts = self.imap.uid('FETCH', uid, '(BODY.PEEK[])')
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1), None)
                if raw:
                    out.append(parse_rfc822(raw, uid=uid))
            return out
        finally:
            self.imap.logout()


# ── Microsoft Graph ──────────────────────────────────────────────────────────

GRAPH_BASE = 'https://graph.microsoft.com/v1.0'


class GraphMailbox:
    """GET only, on ONE mailbox. `transport.get(url, params, headers) -> dict`."""

    def __init__(self, transport, mailbox: str, folder: str = 'inbox'):
        if not mailbox:
            raise ValueError('INTAKE_GRAPH_MAILBOX is required')
        self.t, self.mailbox, self.folder = transport, mailbox.strip().lower(), folder
        self.prefix = f'{GRAPH_BASE}/users/{quote(self.mailbox, safe="@")}/'

    def _get(self, url, params=None):
        if not url.startswith(self.prefix):
            raise ReadOnlyViolation(f'Graph call outside the configured mailbox: {url}')
        return self.t.get(url, params=params or {},
                          headers={'Prefer': 'outlook.body-content-type="text"'})

    def fetch(self, since: datetime) -> list[RawMessage]:
        url = f'{self.prefix}mailFolders/{quote(self.folder)}/messages'
        params = {'$filter': f'receivedDateTime ge {since.strftime("%Y-%m-%dT%H:%M:%SZ")}',
                  '$select': 'id,internetMessageId,from,subject,receivedDateTime,body,hasAttachments',
                  '$top': '50'}
        out = []
        while url:
            page = self._get(url, params)
            params = None                                       # nextLink carries the query
            for m in page.get('value', []):
                out.append(self._message(m))
            url = page.get('@odata.nextLink')
        return out

    def _message(self, m) -> RawMessage:
        sender = ((m.get('from') or {}).get('emailAddress') or {}).get('address', '').lower()
        received = m.get('receivedDateTime')
        received_at = datetime.fromisoformat(received.replace('Z', '+00:00')) if received else None
        atts = []
        if m.get('hasAttachments'):
            page = self._get(f"{self.prefix}messages/{quote(m['id'])}/attachments")
            for a in page.get('value', []):
                if a.get('@odata.type', '').endswith('fileAttachment') and a.get('contentBytes'):
                    atts.append(RawAttachment(a.get('name') or 'attachment', a.get('contentType') or '',
                                              base64.b64decode(a['contentBytes'])))
        mid = m.get('internetMessageId') or _synthetic_id(sender, m.get('subject'), received, m.get('id'))
        return RawMessage(mid, sender, m.get('subject') or '', received_at,
                          (m.get('body') or {}).get('content', ''), atts, uid='')


class RequestsGraphTransport:
    """Production transport: client-credentials token, then HTTP GET only."""

    def __init__(self, tenant, client_id, secret):
        self.tenant, self.client_id, self.secret, self._token = tenant, client_id, secret, None

    def _auth(self):
        import requests
        if self._token is None:
            r = requests.post(f'https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token',
                              data={'client_id': self.client_id, 'client_secret': self.secret,
                                    'scope': 'https://graph.microsoft.com/.default',
                                    'grant_type': 'client_credentials'}, timeout=30)
            r.raise_for_status()
            self._token = r.json()['access_token']
        return self._token

    def get(self, url, params=None, headers=None):
        import requests
        h = {'Authorization': f'Bearer {self._auth()}', **(headers or {})}
        r = requests.get(url, params=params, headers=h, timeout=60)
        r.raise_for_status()
        return r.json()


# ── Fake + factory ────────────────────────────────────────────────────────────

class FakeMailbox:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.calls = 0

    def fetch(self, since):
        self.calls += 1
        return [m for m in self.messages if m.received_at is None or m.received_at >= since]


def since_window(now=None) -> datetime:
    days = int(os.environ.get('INTAKE_FETCH_SINCE_DAYS', '14') or 14)
    return (now or timezone.now()) - timedelta(days=days)


def get_mailbox() -> Mailbox:
    kind = os.environ.get('INTAKE_MAILBOX_TYPE', '').strip().lower()
    folder = os.environ.get('INTAKE_MAILBOX_FOLDER', '') or None
    if kind == 'imap':
        import imaplib
        conn = imaplib.IMAP4_SSL(os.environ['INTAKE_IMAP_HOST'], int(os.environ.get('INTAKE_IMAP_PORT', '993')))
        auth = os.environ.get('INTAKE_IMAP_AUTH', 'password').lower()
        secret = os.environ.get('INTAKE_IMAP_OAUTH_TOKEN' if auth == 'xoauth2' else 'INTAKE_IMAP_PASSWORD', '')
        return ImapMailbox(conn, os.environ['INTAKE_IMAP_USER'], secret, auth, folder or 'INBOX')
    if kind == 'graph':
        t = RequestsGraphTransport(os.environ['INTAKE_GRAPH_TENANT_ID'], os.environ['INTAKE_GRAPH_CLIENT_ID'],
                                   os.environ['INTAKE_GRAPH_CLIENT_SECRET'])
        return GraphMailbox(t, os.environ['INTAKE_GRAPH_MAILBOX'], folder or 'inbox')
    raise RuntimeError('INTAKE_MAILBOX_TYPE must be imap or graph')
