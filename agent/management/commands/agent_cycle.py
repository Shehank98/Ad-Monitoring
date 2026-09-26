"""agent_cycle (railway/cron-agent.json, every 15 minutes). Phase 3, autonomy level 0.

Observes every due scope (read-only), rehearses it inside the nightly shadow window
(rolled back, PostgreSQL only) and records snapshots, findings and pending effects in
agent tables. See agent/cycle.py. Exit code 1 when the cycle stopped on a write inside a
read-only transaction (stop and ask) or on a service-user error.
"""
import json
import sys

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime

from agent.cycle import run_cycle
from agent.db import CoreWriteAttempt


class Command(BaseCommand):
    help = 'One Reconciliation Agent cycle (level 0: observe + rolled-back shadow runs).'

    def add_arguments(self, parser):
        parser.add_argument('--now', help='ISO datetime to use as "now" (synthetic walk-throughs only)')

    def handle(self, *args, **opts):
        now = parse_datetime(opts['now']) if opts.get('now') else None
        try:
            res = run_cycle(now=now)
        except CoreWriteAttempt as exc:
            self.stderr.write(f'STOP: {exc}')
            sys.exit(1)
        self.stdout.write(json.dumps(res, default=str, indent=1, sort_keys=True))
        if res.get('outcome') == 'service_user_error':
            sys.exit(1)
