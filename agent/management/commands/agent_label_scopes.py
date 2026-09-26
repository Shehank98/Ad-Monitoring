"""agent_label_scopes <csv> --actor <admin email>  (Phase 3 S2c, owner Q4).

Stores the owner's labels on the findings ledger (label_source='owner'). Every row is
checked first; if any row is refused (unknown account, strings that do not match a
Schedule exactly, unknown cause code, bad date), nothing is stored. Each stored row is a
human AgentAction on agent.FindingLedger. Core data is never written.
"""
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from agent import gate
from agent.labels import CAUSE_CODES, apply_label, parse


class Command(BaseCommand):
    help = 'Store owner labels (CSV) on the agent findings ledger.'

    def add_arguments(self, parser):
        parser.add_argument('csv')
        parser.add_argument('--actor', required=True, help='email of a super_admin/admin')

    def handle(self, *args, **opts):
        actor = get_user_model().objects.filter(email__iexact=opts['actor'], is_active=True).first()
        if actor is None or actor.role not in gate.HUMAN_ROLES:
            raise CommandError('--actor must be an active super_admin or admin')
        labels, errors = parse(opts['csv'])
        if errors:
            for e in errors:
                self.stderr.write(e)
            self.stderr.write(f'Refused: {len(errors)} row(s) with problems; nothing stored. '
                              f'Cause codes: {", ".join(CAUSE_CODES)}')
            sys.exit(1)
        n_lab = n_miss = 0
        for lab in labels:
            res = apply_label(lab, actor)
            n_lab += len(res['labelled'])
            n_miss += res['owner_only_row'] is not None
        self.stdout.write(f'{len(labels)} label row(s): {n_lab} finding(s) labelled, '
                          f'{n_miss} owner-only row(s) (not found by the agent).')
