"""Test doubles: only the mailbox transports and the LLM provider are faked."""
import base64
import io
from datetime import datetime, timezone as dt_tz
from email.message import EmailMessage

from intake.mailbox import RawAttachment, RawMessage

WHEN = datetime(2025, 2, 3, 9, 0, tzinfo=dt_tz.utc)


def rfc822(message_id='<m1@tv.lk>', sender='desk@tv.lk', subject='TC January', body='Please find TC.',
           attachments=(('tc.xlsx', b'PK-fake'),), date='Mon, 03 Feb 2025 09:00:00 +0000') -> bytes:
    m = EmailMessage()
    m['From'] = f'TV Desk <{sender}>'
    m['To'] = 'tc@agency.lk'
    m['Subject'] = subject
    m['Date'] = date
    if message_id:
        m['Message-ID'] = message_id
    m.set_content(body)
    for name, data in attachments:
        m.add_attachment(data, maintype='application', subtype='octet-stream', filename=name)
    return m.as_bytes()


class FakeImapConn:
    """imaplib-like. Records every command; the guard must only ever send read-only ones."""

    def __init__(self, messages: dict[str, bytes]):
        self.messages, self.commands = messages, []

    def login(self, user, pw):
        self.commands.append(('LOGIN', user))
        return 'OK', [b'']

    def authenticate(self, mech, cb):
        self.commands.append(('AUTHENTICATE', mech, cb(b'')))
        return 'OK', [b'']

    def select(self, folder, readonly=False):
        self.commands.append(('EXAMINE' if readonly else 'SELECT', folder))
        return 'OK', [b'1']

    def uid(self, cmd, *args):
        self.commands.append(('UID', cmd.upper(), *args))
        if cmd.upper() == 'SEARCH':
            return 'OK', [' '.join(self.messages).encode()]
        if cmd.upper() == 'FETCH':
            return 'OK', [(b'1 (UID %s BODY[] {n}' % args[0].encode(), self.messages[args[0]]), b')']
        return 'OK', [b'']

    def store(self, *a):                       # present on imaplib; the guard must never reach it
        self.commands.append(('STORE',) + a)

    def expunge(self):
        self.commands.append(('EXPUNGE',))

    def logout(self):
        self.commands.append(('LOGOUT',))


class FakeGraphTransport:
    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, headers=None):
        self.calls.append(('GET', url))
        return self.pages.get(url.split('?')[0], {'value': []})


def raw(message_id='<m1@tv.lk>', sender='desk@tv.lk', subject='TC January', body='Please find TC.',
        attachments=(('tc.xlsx', b'PK-fake'),), when=None, uid='') -> RawMessage:
    from django.utils import timezone
    when = when or timezone.now()
    return RawMessage(message_id, sender, subject, when, body,
                      [RawAttachment(n, 'application/octet-stream', d) for n, d in attachments], uid)


def xlsx_bytes(rows, columns=('Channel', 'Date', 'Programme', 'TC_Theme', 'Duration', 'Aired_Time')) -> bytes:
    import pandas as pd
    buf = io.BytesIO()
    pd.DataFrame(rows, columns=list(columns)).to_excel(buf, index=False)
    return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
