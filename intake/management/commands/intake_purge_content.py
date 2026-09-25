"""Retention (owner C6): clear attachment bytes and email bodies of finished items.

Clears InboundAttachment.content and InboundEmail.body_text for items that are
uploaded, ignored, rejected or duplicate and older than --days. Keeps sha256, metadata,
decisions and evidence. One AgentAction per run (system retention write, service user).
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from agent import gate
from agent.service import require_service_user
from intake.models import InboundAttachment, InboundEmail

FINISHED = ('uploaded', 'ignored', 'rejected', 'duplicate')


def purge(days: int, now=None) -> dict:
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    atts = (InboundAttachment.objects.filter(status__in=FINISHED, email__fetched_at__lt=cutoff,
                                             purged_at__isnull=True))
    att_ids = list(atts.values_list('id', flat=True))
    email_ids = list(InboundEmail.objects.filter(fetched_at__lt=cutoff, purged_at__isnull=True)
                     .exclude(attachments__status__in=['new', 'processing', 'needs_review', 'suggested'])
                     .values_list('id', flat=True))

    def apply():
        InboundAttachment.objects.filter(id__in=att_ids).update(content=b'', purged_at=now)
        InboundEmail.objects.filter(id__in=email_ids).update(body_text='', purged_at=now)
        return {'attachments_purged': len(att_ids), 'emails_purged': len(email_ids)}

    # Retention only clears stored bytes/bodies on intake tables; it must keep running even
    # when fetch or the agent is switched off, so it has its own actor kind.
    act = gate.perform(tier=gate.T0, action_type='intake_purge_content', actor_kind='intake_retention',
                       actor=require_service_user(), target_model='intake.InboundAttachment',
                       before={'days': days, 'cutoff': cutoff.isoformat(),
                               'attachment_ids': att_ids, 'email_ids': email_ids},
                       apply=apply, reason=f'retention: clear content older than {days} days')
    return act.after


class Command(BaseCommand):
    help = 'Clear stored attachment bytes and email bodies of finished TC Inbox items older than N days.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=90)

    def handle(self, *args, **opts):
        self.stdout.write(f"purge: {purge(opts['days'])}")
