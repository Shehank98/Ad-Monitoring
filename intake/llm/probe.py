"""LLM tool_choice probe (Phase 3.2 close). Run by a person: `manage.py intake_llm_probe`.

Sends ONE tiny synthetic request (no client data) three ways and records, for each:
supported / unsupported, the HTTP status, the exact API error text, whether submit_decision
was called, and the tokens used. Each call is one LlmCall(purpose='probe') row; nothing else
is written.

    auto   {"type": "auto"}
    tool   {"type": "tool", "name": "submit_decision"}
    any    {"type": "any"}

The intake runner's 'forced' mode uses {"type": "any"} on its normal turns (a tool call every
turn, so detect_tc / find_schedules still work) and {"type": "tool", "name": "submit_decision"}
on its single follow-up turn. So 'forced' needs BOTH `tool` and `any` supported by the latest
probe of the model.
"""
from __future__ import annotations

import time
import uuid

from agent.models import LlmCall

from .provider import ProviderError
from .schemas import TOOLS

MODES = (
    ('auto', {'type': 'auto'}),
    ('tool', {'type': 'tool', 'name': 'submit_decision'}),
    ('any', {'type': 'any'}),
)
FORCED_MODES = ('tool', 'any')
SYSTEM = ('This is a connectivity probe with synthetic data only. Call submit_decision once with '
          'attachment_id 0, decision "needs_review", schedule_id null, reason_code null, '
          'schedule_number_source "none", suspicious_instruction false, note "probe".')
MESSAGE = 'Synthetic probe. There is no attachment and no client data. Submit the probe decision.'
SUBMIT_ONLY = [t for t in TOOLS if t['name'] == 'submit_decision']


def run_probe(provider) -> list[dict]:
    run_id = uuid.uuid4().hex
    out = []
    for mode, choice in MODES:
        t0 = time.monotonic()
        row = {'run_id': run_id, 'mode': mode, 'tool_choice': choice, 'supported': False,
               'http_status': None, 'error_text': '', 'submit_called': False,
               'input_tokens': 0, 'output_tokens': 0}
        try:
            turn = provider.create(SYSTEM, [{'role': 'user', 'content': MESSAGE}], SUBMIT_ONLY,
                                   tool_choice=choice, max_tokens=256)
        except ProviderError as exc:
            row['http_status'] = exc.status_code
            row['error_text'] = exc.error_text
            outcome = 'unsupported' if exc.status_code and 400 <= exc.status_code < 500 else 'error'
        else:
            row.update(supported=True, http_status=200,
                       submit_called=any(b.get('type') == 'tool_use' and b.get('name') == 'submit_decision'
                                         for b in turn.blocks),
                       input_tokens=turn.input_tokens, output_tokens=turn.output_tokens)
            outcome = 'supported'
        row['outcome'] = outcome
        LlmCall.objects.create(
            purpose='probe', provider=getattr(provider, 'name', 'anthropic'),
            model=(getattr(provider, 'model', '') or '')[:80], prompt_version=f'probe:{mode}',
            input_tokens=row['input_tokens'], output_tokens=row['output_tokens'],
            latency_ms=int((time.monotonic() - t0) * 1000), outcome=outcome,
            detail={k: v for k, v in row.items() if k not in ('input_tokens', 'output_tokens')})
        out.append(row)
    return out


def latest_probe(model: str) -> dict | None:
    """{mode: LlmCall} of the most recent probe run for `model`, or None."""
    last = LlmCall.objects.filter(purpose='probe', model=model).order_by('-created_at', '-id').first()
    if last is None:
        return None
    run_id = (last.detail or {}).get('run_id')
    rows = LlmCall.objects.filter(purpose='probe', model=model, detail__run_id=run_id)
    return {(r.detail or {}).get('mode'): r for r in rows}


def forced_supported(model: str) -> tuple[bool, str]:
    """(ok, why). ok only when the latest probe of `model` showed every forced mode supported."""
    if not model:
        return False, 'no model is known (ANTHROPIC_MODEL is not set and no probe has run)'
    rows = latest_probe(model)
    if not rows:
        return False, f'no probe has been run for {model}; run manage.py intake_llm_probe first'
    bad = [m for m in FORCED_MODES if m not in rows or rows[m].outcome != 'supported']
    if bad:
        return False, f'the latest probe of {model} did not show forced tool_choice as supported ({", ".join(bad)})'
    return True, f'the latest probe of {model} showed forced tool_choice as supported'


def current_model() -> str:
    """The model the runner uses (ANTHROPIC_MODEL). The web process may not have that variable
    (it belongs to cron-intake), so fall back to the model of the most recent probe."""
    import os
    m = os.environ.get('ANTHROPIC_MODEL', '')
    if m:
        return m
    last = LlmCall.objects.filter(purpose='probe').order_by('-created_at', '-id').first()
    return last.model if last else ''
