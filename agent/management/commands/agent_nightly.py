"""agent_nightly (railway/cron-audit.json, 23:45 UTC = 05:15 Colombo, after the shadow window;
Phase 3 Q1). Phase 2.1 item 5.

Phase 3.1 T3: first closes tonight's shadow window itself (cycle lock, waiting up to 10 minutes),
so the audit's agent-effect section always has the end fingerprint. On a lock timeout the window
is reported pending and the next night's audit includes it.

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
    help = ('Nightly: close the shadow window (Phase 3.1 T3), core audit, then intake retention purge. '
            'Each step runs; non-zero exit if any failed.')

    def handle(self, *args, **opts):
        failed, done = [], []
        try:                                     # T3: under the cycle lock, waits up to 10 minutes
            from agent.cycle import nightly_close
            res = nightly_close()
            self.stdout.write(f"shadow window: closed {res['closed']}, pending {res['pending']} (lock {res['lock']})")
            done.append('close_shadow_window')
        except BaseException as exc:             # noqa: BLE001 — the audit and purge still run
            failed.append(f'close_shadow_window: {type(exc).__name__}: {exc}')
            self.stderr.write(f'close_shadow_window failed: {exc}')
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
