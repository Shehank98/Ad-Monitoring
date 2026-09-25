"""fetch_tc_emails (cron): read the mailbox, store emails and attachment bytes.

Writes only InboundEmail / InboundAttachment, through gate.perform(actor_kind=
'intake_fetch') — not blocked by the kill switch, but needs intake_fetch_enabled and an
active AllowedSender (owner Q6). Idempotent by message_id (and IMAP UID) and by
attachment sha256. Never reads or writes media, never touches a core table.
"""
from __future__ import annotations

import hashlib

from django.db import transaction
from django.utils import timezone

from agent import gate
from agent.service import require_service_user

from ..mailbox import since_window
from ..models import (
    ACCEPTED_EXTENSIONS, MAX_ATTACHMENT_BYTES, AllowedSender, InboundAttachment, InboundEmail,
)
from .lock import intake_lock


def _ext(name: str) -> str:
    return name.rsplit('.', 1)[-1].lower() if '.' in (name or '') else ''


def store_message(m) -> InboundEmail | None:
    """Store one RawMessage. Returns None when it was already stored."""
    if InboundEmail.objects.filter(message_id=m.message_id).exists():
        return None
    if m.uid and InboundEmail.objects.filter(imap_uid=m.uid, sender=m.sender,
                                             received_at=m.received_at).exists():
        return None
    known = bool(AllowedSender.for_sender(m.sender))
    em = InboundEmail.objects.create(
        message_id=m.message_id, imap_uid=m.uid or '', sender=m.sender, subject=(m.subject or '')[:500],
        received_at=m.received_at, body_text=m.body_text or '')
    new = 0
    for a in m.attachments:
        data = a.data or b''
        sha = hashlib.sha256(data).hexdigest()
        if em.attachments.filter(sha256=sha).exists():
            continue                                      # same file twice in one email
        ext = _ext(a.filename)
        att = InboundAttachment(email=em, filename=(a.filename or 'attachment')[:255], ext=ext,
                                content_type=(a.content_type or '')[:100], size=len(data), sha256=sha)
        original = (InboundAttachment.objects.filter(sha256=sha).exclude(email=em)
                    .order_by('id').first())
        if original is not None:                          # C7
            att.status, att.reason, att.duplicate_of = 'duplicate', 'duplicate_attachment', original
        elif ext not in ACCEPTED_EXTENSIONS:              # C8
            att.status, att.reason = 'ignored', 'unsupported_type'
        elif len(data) > MAX_ATTACHMENT_BYTES:            # C: nothing stored
            att.status, att.reason = 'needs_review', 'too_large'
        elif not known:
            att.status, att.reason, att.content = 'needs_review', 'unknown_sender', data
        else:
            att.status, att.content = 'new', data
            new += 1
        att.save()
    if not m.attachments:
        em.status, em.reason = 'ignored', 'no_tc_attachment'
    elif not known:
        em.status, em.reason = 'needs_review', 'unknown_sender'
    elif new:
        em.status = 'new'
    else:
        em.status = 'ignored'
    em.save(update_fields=['status', 'reason'])
    return em


def fetch_emails(mailbox, now=None) -> dict:
    """One fetch run. Returns counts; {'status': 'busy'} when another run holds the lock."""
    with intake_lock() as got:
        if not got:
            return {'status': 'busy'}
        actor = require_service_user()
        since = since_window(now)
        box = {}

        def apply():
            messages = mailbox.fetch(since)
            stored = []
            for m in messages:
                with transaction.atomic():
                    em = store_message(m)
                if em is not None:
                    stored.append(em.id)
            box.update(seen=len(messages), stored=len(stored))
            return {'email_ids': stored, 'seen': len(messages)}

        gate.perform(tier=gate.T0, action_type='intake_fetch', actor_kind='intake_fetch',
                     actor=actor, target_model='intake.InboundEmail',
                     before={'emails': InboundEmail.objects.count(), 'since': since.isoformat()},
                     apply=apply, reason='read-only mailbox fetch')
        return {'status': 'ok', **box, 'at': timezone.now().isoformat()}
