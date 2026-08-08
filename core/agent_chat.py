"""
Investigation Agent — "Nova".

A live chat a user opens on a specific ScheduleRow they suspect actually aired
but got marked "Not Aired" (usually a programme-name mismatch or a mapping gap).
Nova is conversational, shows its work, and NEVER links a match on its own — it
only creates a ManualMatch after the live user explicitly confirms a candidate
(Tier 3). All DB access goes through the shared deterministic tools in
core/agent_tools.py, which both this agent and the background Reconciliation
Agent reuse.

Gemini: uses the google-genai SDK (GEMINI_API_KEY, model gemini-2.5-flash). The
SDK is imported lazily so this module always loads (and the deterministic tools
stay unit-testable) even where the package/key is absent; the endpoint then
returns a friendly "not configured" message instead of 500-ing.
"""
from __future__ import annotations

import json
import os

from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from core import agent_tools

PERSONA = """\
You are Nova, a sharp, friendly colleague who helps media operations staff figure
out why a scheduled TV spot got marked "Not Aired" when they suspect it actually
did air.

You are talking directly to a human in a live chat. Be conversational, concise,
and show your work — cite actual times/channels/theme names you find, don't
hand-wave. You are NOT filling out a form; you're having a quick back-and-forth
like a coworker who happens to be very fast at cross-checking spreadsheets.

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

If nothing plausible turns up even with a wide window, say so honestly — don't
force a match. A genuine miss is a valid answer.
"""

# Tools Nova may call. Deterministic implementations live in agent_tools.
AVAILABLE_TOOLS = {
    'get_schedule_row_context': agent_tools.get_schedule_row_context,
    'search_lmrb_candidates':   agent_tools.search_lmrb_candidates,
    'check_brand_mapping':      agent_tools.check_brand_mapping,
    'propose_manual_match':     agent_tools.propose_manual_match,
    'audit_brand_mapping':      agent_tools.audit_brand_mapping,
}


def _tool_declarations(types):
    """Build the Gemini function declarations (needs the SDK's types module)."""
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name='get_schedule_row_context',
            description='Get the planned details of a scheduled spot the user is asking about.',
            parameters={'type': 'OBJECT', 'properties': {
                'schedule_row_id': {'type': 'INTEGER'}}, 'required': ['schedule_row_id']},
        ),
        types.FunctionDeclaration(
            name='search_lmrb_candidates',
            description='Search LMRB for possible airings near a planned time, ignoring exact programme name.',
            parameters={'type': 'OBJECT', 'properties': {
                'channel': {'type': 'STRING'}, 'date': {'type': 'STRING'},
                'planned_time': {'type': 'STRING'}, 'window_minutes': {'type': 'INTEGER'}},
                'required': ['channel', 'date', 'planned_time']},
        ),
        types.FunctionDeclaration(
            name='check_brand_mapping',
            description='Check if a brand has an active BrandMapping and what themes it maps to.',
            parameters={'type': 'OBJECT', 'properties': {
                'account_id': {'type': 'INTEGER'}, 'brand': {'type': 'STRING'}},
                'required': ['account_id', 'brand']},
        ),
        types.FunctionDeclaration(
            name='audit_brand_mapping',
            description="Audit a brand's mappings for common mistakes (product-without-duration, wrong-brand theme, unmapped theme).",
            parameters={'type': 'OBJECT', 'properties': {
                'account_id': {'type': 'INTEGER'}, 'brand': {'type': 'STRING'}},
                'required': ['account_id', 'brand']},
        ),
        types.FunctionDeclaration(
            name='propose_manual_match',
            description='Create a manual match, but ONLY when the user has explicitly confirmed this candidate in chat.',
            parameters={'type': 'OBJECT', 'properties': {
                'schedule_row_id': {'type': 'INTEGER'}, 'lmrb_row_id': {'type': 'INTEGER'},
                'confirmed_by_user': {'type': 'BOOLEAN'}},
                'required': ['schedule_row_id', 'lmrb_row_id', 'confirmed_by_user']},
        ),
    ])]


def _dispatch_tool(name, args, user):
    fn = AVAILABLE_TOOLS[name]
    kwargs = dict(args)
    # propose_manual_match is Tier 3 — attach the authenticated user so the
    # ManualMatch records who confirmed it. The agent still cannot act unless
    # confirmed_by_user is True (enforced inside the tool).
    if name == 'propose_manual_match':
        kwargs['user'] = user
    return fn(**kwargs)


def _run_turn(conversation, user, max_steps: int = 6):
    """Drive one user turn: let Gemini call tools until it produces text.
    Returns (reply_text, updated_conversation_as_dicts)."""
    from google import genai            # lazy: keeps module import-safe
    from google.genai import types

    client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    contents = [
        types.Content(role=c['role'],
                      parts=[types.Part(text=p['text']) for p in c['parts']])
        for c in conversation
    ]
    for _ in range(max_steps):
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=PERSONA, tools=_tool_declarations(types)),
        )
        candidate = response.candidates[0]
        contents.append(candidate.content)
        calls = [p.function_call for p in candidate.content.parts if p.function_call]
        if not calls:
            text = ''.join(p.text for p in candidate.content.parts if p.text)
            return text, _contents_to_dicts(contents)
        tool_parts = []
        for call in calls:
            result = _dispatch_tool(call.name, dict(call.args), user)
            tool_parts.append(types.Part(function_response=types.FunctionResponse(
                name=call.name, response=result)))
        contents.append(types.Content(role='tool', parts=tool_parts))
    return ("I'm going back and forth too much on this one — can you tell me more "
            "specifically what you're checking?"), _contents_to_dicts(contents)


def _contents_to_dicts(contents):
    """Serialise only the plain-text turns back to the session (tool call/result
    parts are transient and rebuilt each turn from context we keep in text)."""
    out = []
    for c in contents:
        texts = [p.text for p in getattr(c, 'parts', []) if getattr(p, 'text', None)]
        if texts and c.role in ('user', 'model'):
            out.append({'role': c.role, 'parts': [{'text': t} for t in texts]})
    return out


@login_required
@require_POST
def chat_with_nova(request):
    """POST {"schedule_row_id": 123, "message": "why is this not aired?"}
    -> {"reply": "..."}"""
    try:
        body = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'reply': 'Sorry, I could not read that message.'}, status=400)

    schedule_row_id = body.get('schedule_row_id')
    user_message = (body.get('message') or '').strip()
    if not schedule_row_id or not user_message:
        return JsonResponse({'reply': 'Please tell me which spot and what you want to check.'},
                            status=400)

    if not (os.environ.get('GEMINI_API_KEY') or ''):
        return JsonResponse({'reply': "Nova isn't configured yet — set GEMINI_API_KEY "
                                      "to enable the investigation chat."})

    session_key = f'nova_chat_{schedule_row_id}'
    history = request.session.get(session_key, [])
    if not history:
        history.append({'role': 'user', 'parts': [{'text':
            f'I\'m looking at schedule_row_id={schedule_row_id}. {user_message}'}]})
    else:
        history.append({'role': 'user', 'parts': [{'text': user_message}]})

    try:
        reply, history = _run_turn(history, request.user)
    except ModuleNotFoundError:
        return JsonResponse({'reply': "Nova's AI backend (google-genai) isn't installed "
                                      "in this environment yet."})
    except Exception as exc:  # pragma: no cover - network/LLM failure surface
        return JsonResponse({'reply': f'Nova hit an error talking to the AI backend: {exc}'},
                            status=502)

    request.session[session_key] = history
    return JsonResponse({'reply': reply})
