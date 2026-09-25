"""
Reconciliation Agent preview pages (/dashboard/agent/). Read-only: GET only.

These pages show the agent's view of every scope — state, missing inputs,
mapping gaps, per-schedule status and the Summary Sheet numbers — using only
reads of core data. Nothing here writes to a core table. Actions link to the
existing core pages (uploads, Brand Mappings, Summary Sheet, TC reconcile), as
AGENT_BUILD_BRIEF.md §12 requires.
"""
from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render

from accounts.decorators import role_required
from core.models import AuditLog, get_setting, get_setting_int, get_setting_list
from core.views import _account_access, _account_qs
from verification.tc_engine import build_summary_data

from .scopes import (
    STATE_LABEL, STATES, available_months, build_scope, build_scopes,
    spot_strip, state_counts,
)

# Channel officers keep their own portal; the agent pages are for staff.
AGENT_ROLES = ['super_admin', 'admin', 'team_head', 'planner', 'operations']
QUEUE_ROLES = ['super_admin', 'admin', 'team_head']
ADMIN_ROLES = ['super_admin', 'admin']


def _scope_url(sc) -> str:
    return '/dashboard/agent/scope/?' + urlencode(
        {'account_id': sc.account_id, 'channel': sc.channel, 'month': sc.month})


def _summary_url(sc, schedule_id=None) -> str:
    q = {'account_id': sc.account_id, 'channel': sc.channel, 'month': sc.month}
    if schedule_id:
        q['schedule_id'] = schedule_id
    return '/dashboard/summary/?' + urlencode(q)


def _month_context(request):
    accounts = _account_qs(request.user)
    account_ids = list(accounts.values_list('id', flat=True))
    months = available_months(account_ids)
    month = request.GET.get('month') or (months[0] if months else '')
    if month and month not in months:
        month = months[0] if months else ''
    return accounts, account_ids, months, month


def _next_action(sc) -> tuple[str, str]:
    """What a person (or later the agent) should do next, and where."""
    s = sc.state
    if s == 'WAITING_INPUTS':
        if sc.reason in ('no_tc',):
            return 'Waiting for the channel to send the TC', '/dashboard/tc/upload/'
        if sc.reason == 'tc_not_linked':
            return 'Link the uploaded TC to its schedule', '/dashboard/tc/'
        return 'Upload MediaWatch data for this period', '/dashboard/monitoring/upload/'
    if s == 'NEEDS_HUMAN':
        return 'Resolve the schedule set (duplicate number or locked schedule)', '/dashboard/schedules/'
    if s == 'MAPPING':
        return f'Map {len(sc.unmapped)} brand(s) to a TC theme', '/dashboard/brand-mappings/'
    if s == 'RECONCILING':
        return 'Run TC reconciliation', _summary_url(sc)
    if s == 'READY_FOR_SIGNOFF':
        return 'Review and authorise the Summary Sheet', _summary_url(sc)
    if s == 'AUTHORISED':
        return f'Authorised by {sc.authorised_by}', _summary_url(sc)
    return 'Period still running', ''


@login_required
@role_required(AGENT_ROLES)
def overview(request):
    accounts, account_ids, months, month = _month_context(request)
    scopes = build_scopes(account_ids, month) if month else []
    counts = state_counts(scopes)
    by_state = {c['key']: c['count'] for c in counts}

    attention = [s for s in scopes if s.state in ('MAPPING', 'WAITING_INPUTS', 'RECONCILING')]
    attention.sort(key=lambda s: (['MAPPING', 'RECONCILING', 'WAITING_INPUTS'].index(s.state), s.account_name))
    for s in attention:
        s.url = _scope_url(s)
        s.next_text, s.next_url = _next_action(s)

    clients = {}
    for s in scopes:
        c = clients.setdefault(s.account_name, {'name': s.account_name, 'total': 0, 'done': 0, 'ready': 0})
        c['total'] += 1
        c['done'] += s.state == 'AUTHORISED'
        c['ready'] += s.state == 'READY_FOR_SIGNOFF'
    client_rows = sorted(clients.values(), key=lambda c: c['name'])
    for c in client_rows:
        c['pct'] = round(c['done'] * 100 / c['total']) if c['total'] else 0

    schedules = sum(len(s.schedules) for s in scopes)
    kpis = [
        ('Scopes', len(scopes), f'{schedules} active schedules · {month}'),
        ('Authorised', by_state.get('AUTHORISED', 0),
         f"{round(by_state.get('AUTHORISED', 0) * 100 / len(scopes)) if scopes else 0}% of scopes"),
        ('Ready for sign-off', by_state.get('READY_FOR_SIGNOFF', 0), 'Inputs complete and mapped'),
        ('Needs mapping', by_state.get('MAPPING', 0),
         f"{sum(len(s.unmapped) for s in scopes)} unmapped brand/duration pairs across all scopes"),
        ('Waiting for inputs', by_state.get('WAITING_INPUTS', 0), 'TC or LMRB missing'),
    ]
    return render(request, 'agent/overview.html', {
        'months': months, 'month': month, 'kpis': kpis, 'counts': counts,
        'attention': attention[:10], 'attention_total': len(attention),
        'clients': client_rows, 'activity': _activity_qs(request.user)[:6],
        'agent_enabled': False,
    })


@login_required
@role_required(AGENT_ROLES)
def scope_list(request):
    accounts, account_ids, months, month = _month_context(request)
    state = request.GET.get('state', '')
    acc = request.GET.get('account', '')
    q = request.GET.get('q', '').strip().lower()
    scopes = build_scopes(account_ids, month) if month else []
    counts = state_counts(scopes)
    if state:
        scopes = [s for s in scopes if s.state == state]
    if acc:
        scopes = [s for s in scopes if str(s.account_id) == acc]
    if q:
        scopes = [s for s in scopes if q in f'{s.account_name} {s.channel} {" ".join(s.schedule_numbers)}'.lower()]
    for s in scopes:
        s.url = _scope_url(s)
        s.next_text, s.next_url = _next_action(s)
    return render(request, 'agent/scopes.html', {
        'months': months, 'month': month, 'scopes': scopes, 'counts': counts,
        'state': state, 'account': acc, 'q': request.GET.get('q', ''),
        'accounts': accounts, 'states': STATES,
    })


STEPS = ['Inputs', 'Mapping', 'Reconcile', 'Validate', 'Sign-off', 'Authorised']
STEP_INDEX = {'STILL_AIRING': 0, 'WAITING_INPUTS': 0, 'STANDALONE': 0, 'NEEDS_HUMAN': 0,
              'MAPPING': 1, 'RECONCILING': 2, 'READY_FOR_SIGNOFF': 4, 'AUTHORISED': 6}


@login_required
@role_required(AGENT_ROLES)
def scope_detail(request):
    account_id = request.GET.get('account_id', '')
    channel = request.GET.get('channel', '')
    month = request.GET.get('month', '')
    if not (account_id and channel and month):
        return redirect('/dashboard/agent/scopes/')
    if not _account_access(request.user, account_id):
        return render(request, '403.html', status=403)
    account = _account_qs(request.user).filter(id=account_id).first()
    if account is None:
        raise Http404
    sc = build_scope(account.id, account.name, channel, month)
    if not sc.schedules:
        raise Http404('No schedules in this scope')
    sc.url = _scope_url(sc)
    sc.next_text, sc.next_url = _next_action(sc)

    # Summary per active schedule (build_summary_data is read-only; per-schedule
    # avoids counting superseded versions — discrepancy D5).
    summaries = []
    for st in sc.schedules:
        data = build_summary_data(sc.account_id, sc.channel, sc.month, schedule_id=st.schedule.id)
        summaries.append({'status': st, 'data': data, 'url': _summary_url(sc, st.schedule.id)})

    idx = STEP_INDEX[sc.state]
    steps = [{'n': i + 1, 'label': s, 'cls': 'done' if i < idx else ('now' if i == idx else '')}
             for i, s in enumerate(STEPS)]
    return render(request, 'agent/scope_detail.html', {
        'sc': sc, 'steps': steps, 'summaries': summaries, 'strip': spot_strip(sc),
        'tolerance': get_setting_int('tc_lmrb_time_tolerance', 5),
    })


@login_required
@role_required(QUEUE_ROLES)
def queue(request):
    """Findings the agent would turn into proposals once enabled (brief §8)."""
    accounts, account_ids, months, month = _month_context(request)
    scopes = build_scopes(account_ids, month) if month else []
    items = []
    for s in scopes:
        s.url = _scope_url(s)
        for brand, dur in s.unmapped:
            items.append({'kind': 'Mapping', 'tone': 'agent', 'scope': s,
                          'title': f'{brand} ({dur}s) has no TC theme mapping',
                          'detail': 'Counts as Missed on the Summary Sheet until mapped (R1).',
                          'link': '/dashboard/brand-mappings/quick/', 'link_label': 'Quick Map'})
        if s.reason == 'tc_not_linked':
            items.append({'kind': 'TC link', 'tone': 'warn', 'scope': s,
                          'title': 'A TC is uploaded but not linked to a schedule',
                          'detail': 'Only a linked TC counts toward a schedule number.',
                          'link': '/dashboard/tc/', 'link_label': 'TC reports'})
        for f in s.findings:
            items.append({'kind': 'Check', 'tone': f.tone, 'scope': s, 'title': f.text,
                          'detail': '', 'link': f.link, 'link_label': f.link_label})
    kinds = {}
    for it in items:
        kinds[it['kind']] = kinds.get(it['kind'], 0) + 1
    return render(request, 'agent/queue.html', {
        'months': months, 'month': month, 'items': items, 'kinds': kinds,
    })


def _activity_qs(user):
    qs = AuditLog.objects.select_related('user').order_by('-timestamp')
    if user.role not in ADMIN_ROLES:
        qs = qs.filter(user=user)
    return qs


@login_required
@role_required(AGENT_ROLES)
def activity(request):
    page = Paginator(_activity_qs(request.user), 40).get_page(request.GET.get('page'))
    return render(request, 'agent/activity.html', {'page': page})


@login_required
@role_required(ADMIN_ROLES)
def config(request):
    """Agent settings. Read-only until Phase 1 adds AgentConfig."""
    core_settings = [
        ('TC ↔ LMRB time tolerance', f"±{get_setting_int('tc_lmrb_time_tolerance', 5)} s", 'tc_lmrb_time_tolerance'),
        ('Sponsorship keywords', ', '.join(get_setting_list('lmrb_sponsorship_keywords')) or '—', 'lmrb_sponsorship_keywords'),
        ('Extra TC theme columns', ', '.join(get_setting_list('tc_extra_theme_aliases')) or 'None', 'tc_extra_theme_aliases'),
        ('Extra TC time columns', ', '.join(get_setting_list('tc_extra_time_aliases')) or 'None', 'tc_extra_time_aliases'),
    ]
    agent_settings = [
        ('Agent', 'Disabled', 'The agent does not run yet. Pages show a read-only preview.'),
        ('Autonomy level', '0 · read only', 'Levels 1–3 arrive in Phase 4 and stay off until you raise them.'),
        ('Mapping auto-apply threshold', '0.92', 'Proposals below this confidence go to the review queue.'),
        ('Email TC intake', 'Off', 'Off → suggest → auto (Phase 2).'),
        ('Grace days after period end', '3', 'Days to wait for late TC and LMRB before flagging.'),
    ]
    return render(request, 'agent/config.html', {
        'core_settings': core_settings, 'agent_settings': agent_settings,
        'today': date.today(), 'nova_chat': get_setting('nova_enabled', '1') != '0',
    })
