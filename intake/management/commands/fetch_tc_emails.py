from django.core.management.base import BaseCommand

from agent import gate
from intake.cron.fetch import fetch_emails
from intake.mailbox import get_mailbox


class Command(BaseCommand):
    help = 'Fetch TC emails from the read-only mailbox into the TC Inbox (never uploads).'

    def handle(self, *args, **opts):
        try:
            res = fetch_emails(get_mailbox())
        except gate.FetchDisabled as exc:
            self.stdout.write(f'fetch skipped: {exc}')
            return
        self.stdout.write(f'fetch: {res}')
        if res.get('status') == 'error':
            raise SystemExit(1)
