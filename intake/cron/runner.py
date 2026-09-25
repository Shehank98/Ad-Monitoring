"""
run_tc_intake_agent (cron). One attachment at a time (owner C1).

  R = rules.evaluate(ctx)                 code decides
  L = LLM loop (<= 15 tool calls)         may only downgrade
  final = rules.combine(R, L, ...)        owner C2 table
  write status through gate.perform(actor_kind='intake_runner')
        (kill switch + tc_intake_mode == 'suggest'; never uploads, never reconciles)

The LLM sees only (C5): sender, subject, file name, the body truncated to 4,000
characters, and tool results — all inside <data> tags. Never the file content: the
tools return counts, a date range, the channel guess and at most 20 sample themes.
Rules-only mode (no model, daily token cap reached, provider error) uses R and
downgrades a proposal to needs_review.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from django.db.models import Sum
from django.utils import timezone

from agent import gate
from agent.models import AgentConfig, LlmCall
from agent.service import require_service_user

from ..llm.provider import ProviderError, default_provider
from ..llm.schemas import LLM_TOOL_NAMES, TOOLS, Decision
from ..models import InboundAttachment
from . import rules
from .lock import intake_lock
from .tools import ToolContext, check_brand_overlap, detect_tc, find_schedules, get_schedule

MAX_TOOL_CALLS = 15            # per attachment
BODY_LIMIT = 4000              # C5
PROMPT_PATH = Path(__file__).resolve().parents[1] / 'prompts' / 'tc_intake_system.md'


def load_prompt() -> tuple[str, str]:
    text = PROMPT_PATH.read_text(encoding='utf-8')
    version = next((ln.split(':', 1)[1].strip() for ln in text.splitlines() if ln.startswith('version:')), '')
    return text, version


def _data(text) -> str:
    """Data is never instructions: neutralise any <data> tag inside it before wrapping."""
    s = str(text if text is not None else '')
    return s.replace('<data', '&lt;data').replace('</data', '&lt;/data')


def first_message(att) -> str:
    em = att.email
    body = (em.body_text or '')[:BODY_LIMIT]
    return (f'attachment_id: {att.id}\n'
            '<data>\n'
            f'sender: {_data(em.sender)}\n'
            f'subject: {_data(em.subject)}\n'
            f'file_name: {_data(att.filename)}\n'
            f'body:\n{_data(body)}\n'
            '</data>')


def _tool_result(ctx, name, args) -> dict:
    if name == 'detect_tc':
        return detect_tc(ctx)
    if name == 'find_schedules':
        return find_schedules(ctx)
    if name == 'get_schedule':
        return get_schedule(ctx, args.get('schedule_id'))
    if name == 'check_brand_overlap':
        return check_brand_overlap(ctx, args.get('schedule_id'))
    return {'error': f'unknown tool {name}'}


def tokens_used_today() -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    agg = LlmCall.objects.filter(created_at__gte=start).aggregate(i=Sum('input_tokens'), o=Sum('output_tokens'))
    return (agg['i'] or 0) + (agg['o'] or 0)


def daily_cap() -> int:
    env = os.environ.get('AGENT_LLM_DAILY_TOKEN_CAP', '').strip()
    if env.isdigit():
        return int(env)
    cfg = AgentConfig.objects.filter(pk=1).first()
    return cfg.llm_daily_token_cap if cfg else AgentConfig._meta.get_field('llm_daily_token_cap').default


class LlmFailed(Exception):
    pass


def llm_loop(ctx, provider, system, version) -> dict:
    """Returns the validated decision dict. Raises LlmFailed on any problem."""
    att = ctx.attachment
    messages = [{'role': 'user', 'content': first_message(att)}]
    calls = 0
    while True:
        t0 = time.monotonic()
        try:
            turn = provider.create(system, messages, TOOLS)
        except ProviderError as exc:
            _log(provider, version, 0, 0, t0, 'provider_error')
            raise LlmFailed(f'provider_error: {exc}') from exc
        _log(provider, version, turn.input_tokens, turn.output_tokens, t0, turn.stop_reason)
        if turn.stop_reason == 'refusal':
            raise LlmFailed('refusal')
        messages.append({'role': 'assistant', 'content': turn.blocks})
        uses = [b for b in turn.blocks if b.get('type') == 'tool_use']
        if not uses:
            raise LlmFailed('no_decision')
        results = []
        for u in uses:
            calls += 1
            if calls > MAX_TOOL_CALLS:
                raise LlmFailed('tool_call_limit')
            name, args = u.get('name'), u.get('input') or {}
            if name not in LLM_TOOL_NAMES:
                results.append({'type': 'tool_result', 'tool_use_id': u['id'], 'is_error': True,
                                'content': f'unknown tool {name}'})
                continue
            if name == 'submit_decision':
                args = {**args, 'note': str(args.get('note') or '')[:200]}
                try:
                    d = Decision(**args).model_dump()
                except Exception as exc:      # noqa: BLE001 — pydantic ValidationError / TypeError
                    raise LlmFailed(f'invalid_decision: {type(exc).__name__}') from exc
                if d['attachment_id'] != att.id:
                    raise LlmFailed('wrong_attachment_id')
                if d['schedule_id'] is not None and d['schedule_id'] not in ctx.returned_ids:
                    raise LlmFailed('unknown_schedule_id')
                return d
            schema = next(t for t in TOOLS if t['name'] == name)['input_schema']
            if 'attachment_id' in schema['required'] and args.get('attachment_id') != att.id:
                res = {'error': 'wrong attachment_id'}
            else:
                res = _tool_result(ctx, name, args)
            results.append({'type': 'tool_result', 'tool_use_id': u['id'],
                            'content': f'<data>\n{_data(json.dumps(res, default=str))}\n</data>'})
        messages.append({'role': 'user', 'content': results})


def _log(provider, version, i, o, t0, outcome):
    LlmCall.objects.create(purpose='tc_intake', provider=getattr(provider, 'name', 'anthropic'),
                           model=getattr(provider, 'model', '')[:80], prompt_version=version,
                           input_tokens=i or 0, output_tokens=o or 0,
                           latency_ms=int((time.monotonic() - t0) * 1000), outcome=(outcome or '')[:40])


def process_attachment(att, provider, actor) -> dict:
    ctx = ToolContext(att)
    R = rules.evaluate(ctx)
    system, version = load_prompt()
    L, llm_error, why = None, '', ''
    if provider is None:
        why = 'no model configured'
    elif tokens_used_today() >= daily_cap():
        why = 'daily token cap reached'
    else:
        try:
            L = llm_loop(ctx, provider, system, version)
        except LlmFailed as exc:
            llm_error = str(exc)
    rules_only = L is None
    pdf_single = bool((ctx.detect or {}).get('pdf') and not ctx.detect['pdf'].get('ai_available'))
    final = rules.combine(R, L, rules_only=rules_only, pdf_single_reader=pdf_single,
                          llm_error=llm_error, rules_only_why=why)
    if llm_error == 'unknown_schedule_id' and R['decision'] == 'propose':
        final['why'] = 'the assistant named a schedule id no tool returned; rejected'
    evidence = {k: v for k, v in (R.get('evidence') or {}).items()}
    evidence.update(final=final, rules_only=rules_only, llm_error=llm_error, rules_only_why=why,
                    prompt_version=version)

    def apply():
        att.status = final['status']
        att.reason = final['reason']
        att.suggested_schedule_id = final['schedule_id']
        att.llm_hint_schedule_id = final.get('hint_schedule_id')
        att.rule_verdict = {k: R[k] for k in ('decision', 'reason', 'schedule_id')}
        att.llm_verdict = L or ({'error': llm_error} if llm_error else {})
        att.note = ((L or {}).get('note') or final['why'])[:300]
        att.evidence = json.loads(json.dumps(evidence, default=str))
        att.decided_at = timezone.now()
        att.save()
        _email_status(att.email)
        return {'attachment_id': att.id, 'status': att.status, 'reason': att.reason,
                'schedule_id': att.suggested_schedule_id}
    gate.perform(tier=gate.T0, action_type='intake_decision', actor_kind='intake_runner', actor=actor,
                 target_model='intake.InboundAttachment', target_pk=att.id,
                 before={'status': att.status, 'reason': att.reason}, apply=apply,
                 reason=final['why'], evidence={'rule': R['decision'], 'llm': (L or {}).get('decision')})
    return final


def _email_status(em):
    statuses = set(em.attachments.values_list('status', flat=True))
    for s in ('needs_review', 'suggested', 'new', 'uploaded', 'ignored', 'duplicate', 'rejected'):
        if s in statuses:
            em.status = s
            break
    em.save(update_fields=['status'])


def run_pending(provider='default', limit: int = 50) -> dict:
    """Process new attachments. Needs the agent enabled and tc_intake_mode == 'suggest'
    (checked by the gate on every write; checked here first to skip cleanly)."""
    cfg = AgentConfig.objects.filter(pk=1).first()
    if not cfg or not cfg.enabled or cfg.tc_intake_mode != 'suggest':
        return {'status': 'skipped', 'reason': 'agent disabled or intake mode is not suggest'}
    with intake_lock() as got:
        if not got:
            return {'status': 'busy'}
        actor = require_service_user()
        if provider == 'default':
            try:
                provider = default_provider()
            except ProviderError:
                provider = None
        done = []
        for att in (InboundAttachment.objects.filter(status='new', purged_at__isnull=True)
                    .select_related('email').order_by('id')[:limit]):
            done.append(process_attachment(att, provider, actor)['status'])
        return {'status': 'ok', 'processed': len(done), 'results': done}
