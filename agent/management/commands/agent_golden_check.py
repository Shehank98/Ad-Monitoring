"""python manage.py agent_golden_check snapshot --scopes FILE --out FILE
python manage.py agent_golden_check verify --mode idempotent --scopes FILE [--report FILE]
AGENT_DISPOSABLE_DB=1 python manage.py agent_golden_check verify --mode rebuild --scopes FILE [--baseline FILE]

See agent/golden.py. Every run is inside a transaction that always rolls back.
"""
import json

from django.core.management.base import BaseCommand, CommandError

from agent import golden
from agent.canonical import canonical_json


class Command(BaseCommand):
    help = 'Golden regression check for the Reconciliation Agent (Amendment A4/A11/P3).'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['snapshot', 'verify'])
        parser.add_argument('--scopes', required=True, help='JSON: [{"account", "channel", "month"}, …]')
        parser.add_argument('--mode', choices=['idempotent', 'rebuild'], default='idempotent')
        parser.add_argument('--out', default='', help='snapshot: where to write the baseline JSON')
        parser.add_argument('--baseline', default='', help='rebuild: baseline JSON from snapshot')
        parser.add_argument('--report', default='', help='write the markdown report here')

    def handle(self, *args, **o):
        try:
            entries = golden.load_entries(o['scopes'])
            if o['action'] == 'snapshot':
                data = golden.snapshot(entries)
                text = canonical_json(data)
                if o['out']:
                    with open(o['out'], 'w', encoding='utf-8') as fh:
                        fh.write(text)
                    self.stdout.write(f"Baseline for {len(data['scopes'])} scope(s) written to {o['out']}")
                else:
                    self.stdout.write(text)
                return
            if o['mode'] == 'idempotent':
                report = golden.verify_idempotent(entries)
            else:
                baseline = None
                if o['baseline']:
                    with open(o['baseline'], encoding='utf-8') as fh:
                        baseline = json.load(fh)
                report = golden.verify_rebuild(entries, baseline)
        except golden.GoldenRefused as exc:
            raise CommandError(str(exc))
        md = golden.render(report)
        if o['report']:
            with open(o['report'], 'w', encoding='utf-8') as fh:
                fh.write(md)
        self.stdout.write(md)
        if o['mode'] == 'idempotent' and not report['ok']:
            raise CommandError('Golden check MISMATCH (stop and ask, A12).')
