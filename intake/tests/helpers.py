"""Synthetic intake scenario: one client, one schedule, one mapped brand, one allowed sender."""
import hashlib
from datetime import date

from agent.models import AgentConfig
from agent.service import ensure_service_user
from agent.tests import factories as f
from intake.models import AllowedSender, InboundAttachment, InboundEmail

from .fakes import xlsx_bytes


def tc_rows(n=4, theme='NEXUS 30', channel='Sirasa TV', month=1, day0=10, dur=30):
    return [[channel, date(2025, month, day0 + i).isoformat(), 'News', theme, dur, f'20:0{i}:00']
            for i in range(n)]


def enable(mode='suggest', enabled=True):
    c = AgentConfig.get_solo()
    c.enabled, c.tc_intake_mode, c.intake_fetch_enabled = enabled, mode, True
    c.save()
    return ensure_service_user()[0]


def scenario(rows=None, sender='desk@tv.lk', subject='TC Sirasa January', body='TC attached.',
             filename='tc_jan.xlsx', data=None, allowed='tv.lk'):
    acc = f.account()
    s = f.schedule(acc)                                  # Sirasa TV, January 2025, #101
    f.row(acc, s, brand='Nexus', day=10)
    f.mapping(acc, brand='Nexus', tc='NEXUS 30')
    if allowed:
        AllowedSender.objects.get_or_create(email_or_domain=allowed)
    att = attachment(rows=rows, sender=sender, subject=subject, body=body, filename=filename, data=data)
    return acc, s, att


def attachment(rows=None, sender='desk@tv.lk', subject='TC', body='', filename='tc.xlsx', data=None,
               message_id=None):
    data = data if data is not None else xlsx_bytes(rows if rows is not None else tc_rows())
    n = InboundEmail.objects.count() + 1
    em = InboundEmail.objects.create(message_id=message_id or f'<m{n}@tv.lk>', sender=sender,
                                     subject=subject, body_text=body)
    return InboundAttachment.objects.create(
        email=em, filename=filename, ext=filename.rsplit('.', 1)[-1].lower(), size=len(data),
        sha256=hashlib.sha256(data).hexdigest(), content=data, status='new')
