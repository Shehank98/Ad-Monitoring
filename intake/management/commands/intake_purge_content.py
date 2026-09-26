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

FINISHED = ('uploaded', 'ignored', 'rejected', 'duplicate', 'expired')


MIN_DAYS = 7
REVIEW_DAYS_DEFAULT, MIN_REVIEW_DAYS = 180, 30
OPEN = ('new', 'processing', 'needs_review', 'suggested')


def purge(days: int, review_days: int = REVIEW_DAYS_DEFAULT, now=None) -> dict:
    """1. needs_review items older than review_days -> status expired, reason retention_expired
          (Phase 2.1 item 5), content cleared.
       2. finished items (uploaded / ignored / rejected / duplicate / expired) older than days:
          attachment bytes and email bodies cleared. sha256, metadata, decisions, evidence kept."""
    if days < MIN_DAYS:
        raise ValueError(f'--days must be at least {MIN_DAYS}')
    if review_days < MIN_REVIEW_DAYS:
        raise ValueError(f'--review-days must be at least {MIN_REVIEW_DAYS}')
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    review_cutoff = now - timedelta(days=review_days)
    expire_ids = list(InboundAttachment.objects.filter(status='needs_review', email__fetched_at__lt=review_cutoff)
                      .values_list('id', flat=True))

    def apply():
        InboundAttachment.objects.filter(id__in=expire_ids, status='needs_review').update(
            status='expired', reason='retention_expired', content=b'', purged_at=now)
        att_ids = list(InboundAttachment.objects.filter(status__in=FINISHED, email__fetched_at__lt=cutoff,
                                                        purged_at__isnull=True).values_list('id', flat=True))
        InboundAttachment.objects.filter(id__in=att_ids).update(content=b'', purged_at=now)
        email_ids = list(InboundEmail.objects.filter(fetched_at__lt=cutoff, purged_at__isnull=True)
                         .exclude(attachments__status__in=OPEN).values_list('id', flat=True))
        InboundEmail.objects.filter(id__in=email_ids).update(body_text='', purged_at=now)
        return {'expired': len(expire_ids), 'attachments_purged': len(att_ids), 'emails_purged': len(email_ids),
                'expired_ids': expire_ids, 'attachment_ids': att_ids, 'email_ids': email_ids}

    # Retention only clears stored bytes/bodies on intake tables; it must keep running even
    # when fetch or the agent is switched off, so it has its own actor kind.
    act = gate.perform(tier=gate.T0, action_type='intake_purge_content', actor_kind='intake_retention',
                       actor=require_service_user(), target_model='intake.InboundAttachment',
                       before={'days': days, 'review_days': review_days, 'cutoff': cutoff.isoformat()},
                       apply=apply, reason=f'retention: {days} days (finished), {review_days} days (review)')
    return {k: v for k, v in act.after.items() if not k.endswith('_ids')}


class Command(BaseCommand):
    help = 'Clear stored attachment bytes and email bodies of finished TC Inbox items older than N days.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=90)
        parser.add_argument('--review-days', type=int, default=REVIEW_DAYS_DEFAULT)

    def handle(self, *args, **opts):
        self.stdout.write(f"purge: {purge(opts['days'], opts['review_days'])}")
