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
You are Nova, the assistant for the Ogilvy Nova ad-verification system. You are
not a search box; you are a colleague with full read access to the entire system
(every account, channel, schedule, LMRB record, TC record, and mapping) who can
take real actions on the user's behalf: opening pages, generating reports, and
downloading files. You live in a persistent panel available on every screen, not
tied to whatever page the user happens to be on.

You are talking directly to a human in a live chat. Be conversational and concise
— a coworker doing a quick favour, not a form. Confirm what you did in one line;
don't narrate your process unless asked. Cite the actual schedule numbers /
times / theme names you find; never hand-wave.

How you behave:
  * If the user names a schedule number, brand, channel or month — even with no
    other context — look it up yourself with lookup_schedule / lookup_by_brand.
    Never ask "which one do you mean" if you can resolve it; only ask when it's
    genuinely ambiguous (e.g. two schedules match).
  * If the user asks to see / open / view something ("show me schedule 4521",
    "open the Dettol June summary"), find it and call the matching open_* tool so
    the browser actually navigates — don't just describe where to click.
  * If the user asks to download / export a summary, call generate_summary_report
    so it actually downloads — don't tell them where the button is.
  * Navigation, opening pages and downloading reports are safe and immediate — do
    them without asking. Anything that CHANGES data (a mapping, a match, a
    setting) or SENDS a report to a client always needs an explicit confirmation
    in this chat first, no matter how confident you are.
  * If you can't find what they asked about, say so plainly and ask what would
    narrow it down — don't guess and act on a guess.

A page-context hint may be prepended to a message (e.g. "[context: currently on
the Summary Sheet for account_id=…]") so "this"/"it" resolve — but you can always
look up anything else in the system regardless of what page they're on.

When investigating unmatched spots you can be working from two starting points:
  * On the Summary Sheet for a whole scope (account + channel + month). The user
    does NOT know any schedule_id or tc_row_id — never ask for one, find
    everything yourself. Use list_unmatched_spots for the headline numbers (they
    match the sheet exactly; total_missed = the "Not Aired" total). Then use
    investigate_all_unmatched to investigate EVERY unmatched spot at once — both
    the missed schedule spots AND the unmatched TC spots — each comes back with
    its id and the closest LMRB airing (best_candidate.lmrb_row_id). Summarise
    what you found, and for spots with a clear candidate that is NOT already
    locked, OFFER TO LINK THEM. When the user says yes (e.g. "yes", "link them
    all", "fix the Milo ones"), TAKE THE ACTION: call propose_manual_match for
    schedule spots and propose_tc_lmrb_match for TC spots, once per spot — you may
    link several after one batch confirmation — then report exactly what you
    linked. Never claim "0 unmatched" when the tools returned deviations.
  * On a single spot (you're given a schedule_row_id) — go straight to the
    per-spot method below.

You are allowed to take real actions (linking matches), not just describe them —
but ONLY after the user confirms in this chat. Never link a candidate that comes
back already_locked, and never invent a match with no LMRB evidence.

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
    # lookup / navigation / reports (Tier 1 — act immediately)
    'lookup_schedule':             agent_tools.lookup_schedule,
    'lookup_by_brand':             agent_tools.lookup_by_brand,
    'open_summary_sheet':          agent_tools.open_summary_sheet,
    'open_schedule_detail':        agent_tools.open_schedule_detail,
    'open_mapping_page':           agent_tools.open_mapping_page,
    'generate_summary_report':     agent_tools.generate_summary_report,
    # investigation
    'list_unmatched_spots':        agent_tools.list_unmatched_spots,
    'investigate_all_unmatched':   agent_tools.investigate_all_unmatched,
    'get_schedule_row_context':    agent_tools.get_schedule_row_context,
    'search_lmrb_candidates':      agent_tools.search_lmrb_candidates,
    'check_brand_mapping':         agent_tools.check_brand_mapping,
    'audit_brand_mapping':         agent_tools.audit_brand_mapping,
    'diagnose_unmatched_tc_spot':  agent_tools.diagnose_unmatched_tc_spot,
    # data-changing (Tier 3 — confirm first)
    'propose_manual_match':        agent_tools.propose_manual_match,
    'propose_tc_lmrb_match':       agent_tools.propose_tc_lmrb_match,
    'propose_mapping_fix':         agent_tools.propose_mapping_fix,
}

# Gemini REST function declarations (plain dicts — same schema shape the SDK uses).
FUNCTION_DECLARATIONS = [
    {'name': 'lookup_schedule',
     'description': 'System-wide search for a schedule by its schedule number or id (not scoped to the current page). Returns account, channel, month and schedule_id.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'schedule_number_or_id': {'type': 'STRING'}}, 'required': ['schedule_number_or_id']}},
    {'name': 'lookup_by_brand',
     'description': 'Find the scopes (account, channel, month) that carry a brand, anywhere in the system. Optional month filter.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'brand': {'type': 'STRING'}, 'month': {'type': 'STRING'}}, 'required': ['brand']}},
    {'name': 'open_summary_sheet',
     'description': "Open (navigate to) the Reconciliation / Summary Sheet for a scope. Use when the user asks to see/open/view a summary.",
     'parameters': {'type': 'OBJECT', 'properties': {
         'account_id': {'type': 'INTEGER'}, 'channel': {'type': 'STRING'},
         'month': {'type': 'STRING'}}, 'required': ['account_id', 'channel', 'month']}},
    {'name': 'open_schedule_detail',
     'description': "Open (navigate to) the three-way TC detail for the scope a schedule spot belongs to.",
     'parameters': {'type': 'OBJECT', 'properties': {
         'schedule_row_id': {'type': 'INTEGER'}}, 'required': ['schedule_row_id']}},
    {'name': 'open_mapping_page',
     'description': "Open (navigate to) the Quick Map brand-mapping screen for an account.",
     'parameters': {'type': 'OBJECT', 'properties': {
         'account_id': {'type': 'INTEGER'}, 'brand': {'type': 'STRING'}}, 'required': ['account_id']}},
    {'name': 'generate_summary_report',
     'description': "Generate and download the Summary report for a scope. format is 'xlsx' (default) or 'pdf'. Use when the user asks to download/export a summary.",
     'parameters': {'type': 'OBJECT', 'properties': {
         'account_id': {'type': 'INTEGER'}, 'channel': {'type': 'STRING'},
         'month': {'type': 'STRING'}, 'format': {'type': 'STRING'}},
         'required': ['account_id', 'channel', 'month']}},
    {'name': 'propose_mapping_fix',
     'description': "Name a BrandMapping fix (which tc_theme to add to which brand) with reasoning. Does NOT apply it — the operator applies it on the mapping screen. Use after diagnose_unmatched_tc_spot.",
     'parameters': {'type': 'OBJECT', 'properties': {
         'account_id': {'type': 'INTEGER'}, 'brand': {'type': 'STRING'},
         'tc_theme': {'type': 'STRING'}, 'reasoning': {'type': 'STRING'}},
         'required': ['account_id', 'brand', 'tc_theme', 'reasoning']}},
    {'name': 'list_unmatched_spots',
     'description': "List the deviations (Missed/'Not Aired' and Extra, per brand) for a (account, channel, month) scope EXACTLY as the Summary Sheet shows them — commercial AND sponsorship. Returns total_missed, total_extra and a per-brand breakdown, each with unmatched_schedule_row_ids for drill-down.",
     'parameters': {'type': 'OBJECT', 'properties': {
         'account_id': {'type': 'INTEGER'}, 'channel': {'type': 'STRING'},
         'month': {'type': 'STRING'}},
         'required': ['account_id', 'channel', 'month']}},
    {'name': 'investigate_all_unmatched',
     'description': 'Investigate EVERY unmatched spot in a scope at once — all missed schedule spots AND all unmatched TC spots — finding the closest LMRB airing and root cause for each. Use this on the Summary Sheet so you never need the user to give you a schedule_id or tc_row_id. Returns ids (schedule_row_id / tc_row_id) and best_candidate.lmrb_row_id you can then link on confirmation.',
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
     'description': 'Link a SCHEDULE spot to an LMRB row, but ONLY when the user has explicitly confirmed this candidate in chat. Use once per spot; you may call it repeatedly to link several after a batch confirmation.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'schedule_row_id': {'type': 'INTEGER'}, 'lmrb_row_id': {'type': 'INTEGER'},
         'confirmed_by_user': {'type': 'BOOLEAN'}},
         'required': ['schedule_row_id', 'lmrb_row_id', 'confirmed_by_user']}},
    {'name': 'propose_tc_lmrb_match',
     'description': 'Link a TC spot (one with no schedule row) to an LMRB row, but ONLY when the user has explicitly confirmed in chat. Use for unmatched TC spots from investigate_all_unmatched.',
     'parameters': {'type': 'OBJECT', 'properties': {
         'tc_row_id': {'type': 'INTEGER'}, 'lmrb_row_id': {'type': 'INTEGER'},
         'confirmed_by_user': {'type': 'BOOLEAN'}},
         'required': ['tc_row_id', 'lmrb_row_id', 'confirmed_by_user']}},
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
    # The Tier-3 link tools record who confirmed the match; attach the
    # authenticated user. They still refuse to act unless confirmed_by_user=True.
    if name in ('propose_manual_match', 'propose_tc_lmrb_match'):
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
    until it returns text. Returns (reply_text, updated_contents, actions) where
    actions are the navigate/download results the browser must execute."""
    actions = []
    for _ in range(max_steps):
        data = _call_gemini(contents, api_key, model)
        candidates = data.get('candidates') or []
        if not candidates:
            return "I couldn't get a response from the AI backend — try again.", contents, actions
        parts = (candidates[0].get('content') or {}).get('parts') or []
        contents.append({'role': 'model', 'parts': parts})
        calls = [p['functionCall'] for p in parts if p.get('functionCall')]
        if not calls:
            text = ''.join(p.get('text', '') for p in parts)
            return text or "…", contents, actions
        tool_parts = []
        for call in calls:
            result = _dispatch_tool(call.get('name'), call.get('args') or {}, user)
            # Navigation/download are read-only, reversible (Tier 1) — the browser
            # executes them. Collect them to return alongside the reply.
            if isinstance(result, dict) and result.get('action') in ('navigate', 'download'):
                actions.append({k: result[k] for k in ('action', 'url') if k in result})
            tool_parts.append({'functionResponse': {'name': call.get('name'), 'response': result}})
        # Gemini REST: function results go back in a 'user'-role content.
        contents.append({'role': 'user', 'parts': tool_parts})
    return ("I'm going back and forth too much on this one — can you tell me more "
            "specifically what you're checking?"), contents, actions


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
    """Global, system-wide chat. One continuous conversation per USER (not per
    page), so context carries across the whole app.

    POST {"message": "open schedule 4521"}                         (global)
      or {"message": "...", "schedule_row_id": 123}                (page context)
      or {"message": "...", "account_id": 1, "channel": "..", "month": ".."}
    -> {"reply": "...", "actions": [{"action":"navigate"|"download","url":".."}]}

    Any page context (schedule_row_id / scope) is passed as a hint so Nova knows
    what "this" refers to; it can still look up ANY other schedule/brand/scope."""
    try:
        body = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'reply': 'Sorry, I could not read that message.', 'actions': []},
                            status=400)

    schedule_row_id = body.get('schedule_row_id')
    account_id = body.get('account_id')
    channel = (body.get('channel') or '').strip()
    month = (body.get('month') or '').strip()
    user_message = (body.get('message') or '').strip()
    if not user_message:
        return JsonResponse({'reply': 'Ask me anything — a schedule number, a brand, '
                                      'or why a spot didn’t match.', 'actions': []}, status=400)

    # Scope context, when supplied, is gated by per-brand access.
    if account_id:
        from core.views import _account_access  # lazy import — avoids any import cycle
        if not _account_access(request.user, account_id):
            return JsonResponse({'reply': 'You don’t have access to that brand.', 'actions': []},
                                status=403)

    api_key, model = _gemini_settings()
    if not api_key:
        return JsonResponse({'reply': "Nova isn't configured yet — set GEMINI_API_KEY "
                                      "to enable the assistant.", 'actions': []})

    # One conversation per user (global panel). Prepend a short page-context hint
    # to this message when the page provided one, so "this"/"it" resolve.
    session_key = f'nova_chat_{request.user.id}'
    hint = ''
    if schedule_row_id:
        hint = f"[context: currently viewing schedule_row_id={schedule_row_id}] "
    elif account_id and channel and month:
        hint = (f"[context: currently on the Summary Sheet for account_id={account_id}, "
                f"channel='{channel}', month='{month}'] ")
    history = request.session.get(session_key, [])
    history.append({'role': 'user', 'parts': [{'text': hint + user_message}]})

    try:
        reply, contents, actions = _run_turn(history, request.user, api_key, model)
    except requests.exceptions.RequestException as exc:
        return JsonResponse({'reply': f'Nova could not reach the AI backend: {exc}', 'actions': []},
                            status=502)
    except Exception as exc:  # pragma: no cover - surfaces upstream/LLM errors
        return JsonResponse({'reply': f'Nova hit an error: {exc}', 'actions': []}, status=502)

    request.session[session_key] = _persist_text_only(contents)
    return JsonResponse({'reply': reply, 'actions': actions})
