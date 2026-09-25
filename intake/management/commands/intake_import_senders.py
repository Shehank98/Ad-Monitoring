"""intake_import_senders <csv> --actor <admin email>

Admin-run import of AllowedSender rows (owner Q2). CSV columns (header row required):
    email_or_domain, channel_hint, accounts, note
`accounts` is a ';'-separated list of exact Account names (empty = all accounts).
Each created or changed row is logged as one AgentAction (actor_kind='human').
"""
import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from agent import gate
from core.models import Account
from intake.models import AllowedSender


def import_rows(rows, actor) -> list[str]:
    out = []
    for i, r in enumerate(rows, start=2):
        key = (r.get('email_or_domain') or '').strip().lower()
        if not key:
            raise CommandError(f'line {i}: email_or_domain is required')
        names = [n.strip() for n in (r.get('accounts') or '').split(';') if n.strip()]
        accs = list(Account.objects.filter(name__in=names))
        missing = sorted(set(names) - {a.name for a in accs})
        if missing:
            raise CommandError(f'line {i}: unknown account(s): {", ".join(missing)}')
        existing = AllowedSender.objects.filter(email_or_domain=key).first()
        before = _snap(existing) if existing else {}

        def apply(key=key, r=r, accs=accs):
            obj, _ = AllowedSender.objects.get_or_create(email_or_domain=key)
            obj.channel_hint = (r.get('channel_hint') or '').strip()
            obj.note = (r.get('note') or '').strip()
            obj.active = True
            obj.save()
            obj.accounts.set(accs)
            return _snap(obj)
        with transaction.atomic():
            act = gate.perform(tier=gate.T0, action_type='allowed_sender_import', actor_kind='human',
                               actor=actor, target_model='intake.AllowedSender', target_pk=key,
                               before=before, apply=apply, reason='intake_import_senders')
        out.append(f"{'updated' if before else 'created'} {key} (action #{act.id})")
    return out


def _snap(a):
    return {'email_or_domain': a.email_or_domain, 'channel_hint': a.channel_hint, 'active': a.active,
            'note': a.note, 'account_ids': sorted(a.accounts.values_list('id', flat=True))}


class Command(BaseCommand):
    help = 'Import allowed TC senders from a CSV (admin-run; logs AgentActions).'

    def add_arguments(self, parser):
        parser.add_argument('csv_path')
        parser.add_argument('--actor', required=True, help='email of the admin running the import')

    def handle(self, *args, **opts):
        actor = User.objects.filter(email=opts['actor']).first()
        if actor is None:
            raise CommandError(f"no user {opts['actor']}")
        with open(opts['csv_path'], newline='', encoding='utf-8-sig') as fh:
            rows = list(csv.DictReader(fh))
        try:
            for line in import_rows(rows, actor):
                self.stdout.write(line)
        except gate.HumanNotAllowed as exc:
            raise CommandError(str(exc)) from exc
