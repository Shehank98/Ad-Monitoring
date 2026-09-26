"""python manage.py agent_core_audit [--synthetic] [--output PATH]

Strictly read-only (Amendment A10/P4): see agent/core_audit.py. The one exception
(guardian check 17): after the read-only transaction has rolled back, the result is saved
once to agent tables (AgentRun kind='audit') in a separate transaction, for the Phase 2
audit card."""
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from agent.core_audit import agent_effect, collect, render_agent_effect, render_markdown
from agent.models import AgentRun


class Command(BaseCommand):
    help = 'Read-only audit of existing-system issues; writes docs/agent/core_audit_<YYYYMMDD>.md'

    def add_arguments(self, parser):
        parser.add_argument('--output', default='')
        parser.add_argument('--synthetic', action='store_true',
                            help='Mark the report "SYNTHETIC DATA" (title and first line).')

    def handle(self, *args, **opts):
        data = collect()                  # read-only; its transaction has already rolled back
        effect = agent_effect()          # Phase 3: agent tables only
        md = render_markdown(data, synthetic=opts['synthetic']) + render_agent_effect(effect)
        run = save_result(data, md, opts['synthetic'], effect)
        out = Path(opts['output'] or Path(settings.BASE_DIR) / 'docs' / 'agent'
                   / f"core_audit_{date.today():%Y%m%d}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding='utf-8')
        self.stdout.write(f'Wrote {out}; saved AgentRun #{run.id} (kind=audit)')
        for line in md.splitlines():
            if line.startswith('| ') and not line.startswith('| Issue') and '---' not in line:
                self.stdout.write(line)
            if line.startswith('## ') and line != '## Headline':
                break


def counts(data: dict) -> dict:
    return {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in data['sections'].items()}


def save_result(data: dict, md: str, synthetic: bool, effect=None) -> AgentRun:
    """The single write: agent tables only, in its own transaction (guardian check 17)."""
    with transaction.atomic():
        return AgentRun.objects.create(
            kind='audit', status='ok', finished_at=timezone.now(),
            detail={'synthetic': synthetic, 'counts': counts(data), 'report': md,
                    'agent_effect': effect or []})
