"""intake_llm_probe (Phase 3.2 close). Run by a person, never by a cron job.

Needs ANTHROPIC_MODEL and ANTHROPIC_API_KEY. Sends one tiny synthetic request (no client data)
with tool_choice auto, tool (submit_decision) and any, prints what the API answered, and logs
each call as LlmCall(purpose='probe'). Writes nothing else.
"""
import json
import os

from django.core.management.base import BaseCommand, CommandError

from intake.llm.probe import run_probe


def make_provider():
    from intake.llm.provider import AnthropicProvider
    return AnthropicProvider()


class Command(BaseCommand):
    help = 'Probe which tool_choice modes the configured Anthropic model accepts (synthetic data only).'

    def handle(self, *args, **opts):
        missing = [v for v in ('ANTHROPIC_MODEL', 'ANTHROPIC_API_KEY') if not os.environ.get(v)]
        if missing:
            raise CommandError(f'Refused: {", ".join(missing)} not set. The probe needs both.')
        provider = make_provider()
        rows = run_probe(provider)
        self.stdout.write(f'Model: {provider.model}')
        for r in rows:
            self.stdout.write(json.dumps({
                'tool_choice': r['tool_choice'], 'result': r['outcome'], 'http_status': r['http_status'],
                'error_text': r['error_text'], 'submit_decision_called': r['submit_called'],
                'input_tokens': r['input_tokens'], 'output_tokens': r['output_tokens']}, ensure_ascii=False))
        forced = all(r['supported'] for r in rows if r['mode'] in ('tool', 'any'))
        self.stdout.write('Forced tool_choice: ' + ('SUPPORTED — an admin may set it in Agent Settings.'
                                                     if forced else 'NOT supported — keep "auto".'))
