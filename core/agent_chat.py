"""
Investigation Agent — "Nova".

A live chat a user opens on a specific ScheduleRow they suspect actually aired
but got marked "Not Aired" (usually a programme-name mismatch or a mapping gap).
Nova is conversational, shows its work, and NEVER links a match on its own — it
only creates a ManualMatch after the live user explicitly confirms a candidate
(Tier 3). All DB access goes through the shared deterministic tools in
core/agent_tools.py, which both this agent and the future background agent reuse.

Gemini: called over the SAME REST endpoint the TC PDF converter already uses
(verification/tc_converters/gemini_ai.py) — generativelanguage.googleapis.com
with the x-goog-api-key header read from settings.GEMINI_API_KEY. No extra
dependency; if the TC converter works in an environment, so does Nova. When the
key is absent the endpoint degrades gracefully instead of erroring.
"""
from __future__ import annotations

import json

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from core import agent_tools

API_URL_TMPL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
REQUEST_TIMEOUT = 60

PERSONA = """\
You are Nova, a sharp, friendly colleague who helps media operations staff figure
out why a scheduled TV spot got marked "Not Aired" when they suspect it actually
did air.

You are talking directly to a human in a live chat. Be conversational, concise,
and show your work — cite actual times/channels/theme names you find, don't
hand-wave. You are NOT filling out a form; you're having a quick back-and-forth
like a coworker who happens to be very fast at cross-checking spreadsheets.

You can be opened two ways:
  * On the Summary Sheet for a whole scope (account + channel + month). Start by
    calling list_unmatched_spots to see every spot that didn't reconcile, give
    the user a short tally ("12 Not Aired, 3 No Mapping"), then offer to dig into
    any of them by name. Investigate a chosen one with the per-spot method below,
    using its schedule_row_id.
  * On a single spot (you're given a schedule_row_id) — go straight to the
    per-spot method below.

Your method when a user asks about a spot:
1. Pull the ScheduleRow's real details (brand, channel, date, planned time).
2. Search LMRB for anything on that channel/date within a wide time window —
   IGNORE exact programme name matching, that's usually the actual bug
   (programme names differ between sources).
3. Check whether the brand has a BrandMapping — if the theme found in LMRB isn't
   mapped to this brand, say so explicitly; that's very often the real cause,
   not "it didn't air."
4. Present candidates plainly: time, theme name, how close to planned time,
   whether it's already mapped to this brand and whether it's already locked.
5. If the user confirms a candidate is the right one ("yes that's it", "link
   it", "match #2"), call propose_manual_match with confirmed_by_user=true — but
   ONLY after they've explicitly said so in this conversation. Never link
   something on your own initiative.
6. If your investigation traces the root cause back to a mapping problem, run
   audit_brand_mapping and tell the user the exact fix (e.g. "clear the Product
   field on this mapping") — don't just report that it's broken, tell them what
   to do about it.
7. If a spot has no schedule/LMRB match and no obvious mapping problem from
   audit_brand_mapping, run diagnose_unmatched_tc_spot to search the raw theme
   across the WHOLE system before concluding it's a genuine miss — then name the
   specific brand-mapping fix (which brand to add the theme to, or move it to).

If nothing plausible turns up even with a wide window, say so honestly — don't
force a match. A genuine miss is a valid answer.
"""

# Tools Nova may call. Deterministic implementations live in agent_tools.
AVAILABLE_TOOLS = {
    'list_unmatched_spots':       agent_tools.list_unmatched_spots,
    'get_schedule_row_context':   agent_tools.get_schedule_row_context,
    'search_lmrb_candidates':     agent_tools.search_lmrb_candidates,
    'check_brand_mapping':        agent_tools.check_brand_mapping,
    'audit_brand_mapping':        agent_tools.audit_brand_mapping,
    'diagnose_unmatched_tc_spot': agent_tools.diagnose_unmatched_tc_spot,
    'propose_manual_match':       agent_tools.propose_manual_match,
}

# Gemini REST function declarations (plain dicts — same schema shape the SDK uses).
FUNCTION_DECLARATIONS = [
    {'name': 'list_unmatched_spots',
     'description': 'List the unmatched (Not Aired / No Mapping / Programme Mismatch / Late Telecast) spots for a (account, channel, month) scope so you can enumerate and then investigate each by schedule_row_id.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'account_id': {'type': 'INTEGER'}, 'channel': {'type': 'STRING'},
         'month': {'type': 'STRING'}},
         'required': ['account_id', 'channel', 'month']}},
    {'name': 'get_schedule_row_context',
     'description': 'Get the planned details of a scheduled spot the user is asking about.',
     'parameters': {'type': 'OBJECT', 'properties': {'schedule_row_id': {'type': 'INTEGER'}},
                    'required': ['schedule_row_id']}},
    {'name': 'search_lmrb_candidates',
     'description': 'Search LMRB for possible airings near a planned time, ignoring exact programme name.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'channel': {'type': 'STRING'}, 'date': {'type': 'STRING'},
         'planned_time': {'type': 'STRING'}, 'window_minutes': {'type': 'INTEGER'}},
         'required': ['channel', 'date', 'planned_time']}},
    {'name': 'check_brand_mapping',
     'description': 'Check if a brand has an active BrandMapping and what themes it maps to.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'account_id': {'type': 'INTEGER'}, 'brand': {'type': 'STRING'}},
         'required': ['account_id', 'brand']}},
    {'name': 'audit_brand_mapping',
     'description': "Audit a brand's mappings for common mistakes (product-without-duration, wrong-brand theme, unmapped theme).",
     'parameters': {'type': 'OBJECT', 'properties': {
         'account_id': {'type': 'INTEGER'}, 'brand': {'type': 'STRING'}},
         'required': ['account_id', 'brand']}},
    {'name': 'diagnose_unmatched_tc_spot',
     'description': "Search a failed TC spot's raw theme across the WHOLE system to find which brand it should map to (missing/wrong mapping).",
     'parameters': {'type': 'OBJECT', 'properties': {'tc_row_id': {'type': 'INTEGER'}},
                    'required': ['tc_row_id']}},
    {'name': 'propose_manual_match',
     'description': 'Create a manual match, but ONLY when the user has explicitly confirmed this candidate in chat.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'schedule_row_id': {'type': 'INTEGER'}, 'lmrb_row_id': {'type': 'INTEGER'},
         'confirmed_by_user': {'type': 'BOOLEAN'}},
         'required': ['schedule_row_id', 'lmrb_row_id', 'confirmed_by_user']}},
]


def _gemini_settings():
    key = getattr(settings, 'GEMINI_API_KEY', '') or ''
    model = getattr(settings, 'GEMINI_TC_MODEL', '') or 'gemini-2.5-flash'
    return key, model


def _dispatch_tool(name, args, user):
    fn = AVAILABLE_TOOLS.get(name)
    if fn is None:
        return {'error': f'unknown tool {name}'}
    kwargs = dict(args or {})
    # propose_manual_match is Tier 3 — attach the authenticated user so the
    # ManualMatch records who confirmed it. The tool still refuses to act
    # unless confirmed_by_user is True.
    if name == 'propose_manual_match':
        kwargs['user'] = user
    return fn(**kwargs)


def _call_gemini(contents, api_key, model):
    payload = {
        'system_instruction': {'parts': [{'text': PERSONA}]},
        'contents': contents,
        'tools': [{'functionDeclarations': FUNCTION_DECLARATIONS}],
        'generationConfig': {'temperature': 0},
    }
    resp = requests.post(
        API_URL_TMPL.format(model=model),
        headers={'x-goog-api-key': api_key, 'Content-Type': 'application/json'},
        json=payload, timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        detail = ''
        try:
            detail = resp.json().get('error', {}).get('message', '')
        except Exception:
            pass
        raise RuntimeError(f'Gemini API error {resp.status_code}: {detail or resp.text[:200]}')
    return resp.json()


def _run_turn(contents, user, api_key, model, max_steps: int = 6):
    """Drive one user turn over REST: let Gemini call tools (functionCall parts)
    until it returns text. Returns (reply_text, updated_contents)."""
    for _ in range(max_steps):
        data = _call_gemini(contents, api_key, model)
        candidates = data.get('candidates') or []
        if not candidates:
            return "I couldn't get a response from the AI backend — try again.", contents
        parts = (candidates[0].get('content') or {}).get('parts') or []
        contents.append({'role': 'model', 'parts': parts})
        calls = [p['functionCall'] for p in parts if p.get('functionCall')]
        if not calls:
            text = ''.join(p.get('text', '') for p in parts)
            return text or "…", contents
        tool_parts = []
        for call in calls:
            result = _dispatch_tool(call.get('name'), call.get('args') or {}, user)
            tool_parts.append({'functionResponse': {'name': call.get('name'), 'response': result}})
        # Gemini REST: function results go back in a 'user'-role content.
        contents.append({'role': 'user', 'parts': tool_parts})
    return ("I'm going back and forth too much on this one — can you tell me more "
            "specifically what you're checking?"), contents


def _persist_text_only(contents):
    """Keep only plain-text user/model turns in the session; tool call/response
    parts are transient and rebuilt from context each turn."""
    out = []
    for c in contents:
        texts = [p['text'] for p in c.get('parts', []) if p.get('text')]
        if texts and c.get('role') in ('user', 'model'):
            out.append({'role': c['role'], 'parts': [{'text': t} for t in texts]})
    return out


@login_required
@require_POST
def chat_with_nova(request):
    """POST either
        {"schedule_row_id": 123, "message": "why is this not aired?"}   (one spot)
      or
        {"account_id": 1, "channel": "Sirasa TV", "month": "January 2025",
         "message": "which spots didn't match?"}                        (whole scope)
    -> {"reply": "..."}"""
    try:
        body = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'reply': 'Sorry, I could not read that message.'}, status=400)

    schedule_row_id = body.get('schedule_row_id')
    account_id = body.get('account_id')
    channel = (body.get('channel') or '').strip()
    month = (body.get('month') or '').strip()
    user_message = (body.get('message') or '').strip()
    if not user_message:
        return JsonResponse({'reply': 'Ask me something about the spot or scope.'}, status=400)
    if not schedule_row_id and not (account_id and channel and month):
        return JsonResponse({'reply': 'Please open me on a spot or a summary scope first.'},
                            status=400)

    # Per-brand access check for scope-level chats (per-spot access is enforced by
    # the row belonging to an account the tools read; scope needs an explicit gate).
    from core.views import _account_access  # lazy import — avoids any import cycle
    if account_id and not _account_access(request.user, account_id):
        return JsonResponse({'reply': 'You don’t have access to that brand.'}, status=403)

    api_key, model = _gemini_settings()
    if not api_key:
        return JsonResponse({'reply': "Nova isn't configured yet — set GEMINI_API_KEY "
                                      "to enable the investigation chat."})

    if schedule_row_id:
        session_key = f'nova_chat_{schedule_row_id}'
        opener = f"I'm looking at schedule_row_id={schedule_row_id}. {user_message}"
    else:
        session_key = f'nova_scope_{account_id}_{channel}_{month}'
        opener = (f"I'm reviewing the reconciliation Summary Sheet for "
                  f"account_id={account_id}, channel='{channel}', month='{month}'. "
                  f"{user_message}")
    history = request.session.get(session_key, [])
    history.append({'role': 'user', 'parts': [{'text': opener if not history else user_message}]})

    try:
        reply, contents = _run_turn(history, request.user, api_key, model)
    except requests.exceptions.RequestException as exc:
        return JsonResponse({'reply': f'Nova could not reach the AI backend: {exc}'}, status=502)
    except Exception as exc:  # pragma: no cover - surfaces upstream/LLM errors
        return JsonResponse({'reply': f'Nova hit an error: {exc}'}, status=502)

    request.session[session_key] = _persist_text_only(contents)
    return JsonResponse({'reply': reply})
