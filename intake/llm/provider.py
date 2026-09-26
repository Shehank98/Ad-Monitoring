"""LLM provider interface (owner D). Anthropic by default; a fake for tests.

The model is read from ANTHROPIC_MODEL (never hard-coded). A provider returns a neutral
Turn so the runner does not depend on SDK types. tool_choice defaults to 'auto': the system
prompt tells the model to call submit_decision, and strict tool schemas keep the arguments
valid. Phase 3.2 close: the runner may pass a forced tool_choice, but only after
`intake_llm_probe` showed that the model accepts it (AgentConfig.intake_tool_choice).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ProviderError(RuntimeError):
    """status_code: HTTP status when the API answered with an error (None for network errors).
    error_text: the API's own error message, exactly as returned."""

    def __init__(self, msg, status_code=None, error_text=''):
        super().__init__(msg)
        self.status_code, self.error_text = status_code, error_text or str(msg)


AUTO = {'type': 'auto'}


@dataclass
class Turn:
    blocks: list = field(default_factory=list)   # dicts, echoed back unchanged as assistant content
    stop_reason: str = 'end_turn'
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ''


class AnthropicProvider:
    name = 'anthropic'

    def __init__(self, model: str | None = None, client=None):
        self.model = model or os.environ.get('ANTHROPIC_MODEL', '')
        if not self.model:
            raise ProviderError('ANTHROPIC_MODEL is not set')
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client

    def create(self, system: str, messages: list, tools: list, tool_choice: dict | None = None,
               max_tokens: int = 4096) -> Turn:
        import anthropic
        try:
            r = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                            messages=messages, tools=tools, tool_choice=tool_choice or AUTO)
        except anthropic.APIStatusError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            err = body.get('error') if isinstance(body.get('error'), dict) else {}
            raise ProviderError(f'{type(exc).__name__}: {exc}', status_code=exc.status_code,
                                error_text=err.get('message') or exc.message) from exc
        except anthropic.APIError as exc:
            raise ProviderError(f'{type(exc).__name__}: {exc}', error_text=getattr(exc, 'message', str(exc))) from exc
        return Turn(blocks=[b.to_dict() for b in r.content], stop_reason=r.stop_reason or '',
                    input_tokens=r.usage.input_tokens or 0, output_tokens=r.usage.output_tokens or 0,
                    model=r.model or self.model)


class FakeProvider:
    """Scripted turns. Records every request so tests can inspect exactly what was sent."""
    name = 'fake'

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.model = 'fake-model'

    def create(self, system, messages, tools, tool_choice=None, max_tokens=4096) -> Turn:
        import copy
        self.requests.append({'system': system, 'messages': copy.deepcopy(messages),
                              'tools': [t['name'] for t in tools], 'tool_choice': tool_choice or AUTO})
        if not self.script:
            raise ProviderError('fake script exhausted')
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def tool_use(name, input, id=None) -> dict:
    return {'type': 'tool_use', 'id': id or f'tu_{name}_{abs(hash(str(input))) % 10**8}',
            'name': name, 'input': input}


def default_provider():
    """AnthropicProvider when ANTHROPIC_MODEL is set, else None (rules only)."""
    if not os.environ.get('ANTHROPIC_MODEL'):
        return None
    return AnthropicProvider()
