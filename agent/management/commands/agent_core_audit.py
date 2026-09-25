"""python manage.py agent_core_audit [--synthetic] [--output PATH]

Strictly read-only (Amendment A10/P4): see agent/core_audit.py."""
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from agent.core_audit import collect, render_markdown


class Command(BaseCommand):
    help = 'Read-only audit of existing-system issues; writes docs/agent/core_audit_<YYYYMMDD>.md'

    def add_arguments(self, parser):
        parser.add_argument('--output', default='')
        parser.add_argument('--synthetic', action='store_true',
                            help='Mark the report "SYNTHETIC DATA" (title and first line).')

    def handle(self, *args, **opts):
        data = collect()
        md = render_markdown(data, synthetic=opts['synthetic'])
        out = Path(opts['output'] or Path(settings.BASE_DIR) / 'docs' / 'agent'
                   / f"core_audit_{date.today():%Y%m%d}.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding='utf-8')
        self.stdout.write(f'Wrote {out}')
        for line in md.splitlines():
            if line.startswith('| ') and not line.startswith('| Issue') and '---' not in line:
                self.stdout.write(line)
            if line.startswith('## ') and line != '## Headline':
                break
