"""agent_nightly (railway/cron-audit.json, 02:00 Colombo). Phase 2.1 item 5.

Runs agent_core_audit, then intake_purge_content. Each step runs even if the other
fails; the command exits non-zero if any step failed. Heartbeat 'agent_nightly' records
the outcome.
"""
import sys

from django.core.management import call_command
from django.core.management.base import BaseCommand

from agent.heartbeat import beat_error, beat_ok

STEPS = (
    ('agent_core_audit', ['--output', '/tmp/core_audit.md']),
    ('intake_purge_content', ['--days', '90']),
)


class Command(BaseCommand):
    help = 'Nightly: core audit, then intake retention purge. Each step runs; non-zero exit if any failed.'

    def handle(self, *args, **opts):
        failed, done = [], []
        for name, argv in STEPS:
            try:
                call_command(name, *argv, stdout=self.stdout, stderr=self.stderr)
                done.append(name)
            except BaseException as exc:          # noqa: BLE001 — incl. SystemExit; the next step still runs
                failed.append(f'{name}: {type(exc).__name__}: {exc}')
                self.stderr.write(f'{name} failed: {exc}')
        if failed:
            beat_error('agent_nightly', '; '.join(failed))
            sys.exit(1)
        beat_ok('agent_nightly', {'steps': done})
