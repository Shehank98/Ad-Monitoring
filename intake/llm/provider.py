"""LLM provider interface (owner D). Anthropic by default; a fake for tests.

The model is read from ANTHROPIC_MODEL (never hard-coded). A provider returns a neutral
Turn so the runner does not depend on SDK types. tool_choice is 'auto': the system
prompt tells the model to call submit_decision, and strict tool schemas keep the
arguments valid (forced tool_choice is rejected by some current models).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ProviderError(RuntimeError):
    pass


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

    def create(self, system: str, messages: list, tools: list) -> Turn:
        import anthropic
        try:
            r = self.client.messages.create(model=self.model, max_tokens=4096, system=system,
                                            messages=messages, tools=tools, tool_choice={'type': 'auto'})
        except anthropic.APIError as exc:
            raise ProviderError(f'{type(exc).__name__}: {exc}') from exc
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

    def create(self, system, messages, tools) -> Turn:
        import copy
        self.requests.append({'system': system, 'messages': copy.deepcopy(messages),
                              'tools': [t['name'] for t in tools]})
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
