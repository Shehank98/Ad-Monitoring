from django.core.management.base import BaseCommand

from agent import gate
from intake.cron.runner import run_pending


class Command(BaseCommand):
    help = 'Suggest a schedule for each new TC attachment (suggest mode; never uploads or reconciles).'

    def handle(self, *args, **opts):
        try:
            self.stdout.write(f'intake: {run_pending()}')
        except (gate.AgentDisabled, gate.IntakeModeOff) as exc:
            self.stdout.write(f'intake skipped: {exc}')
