"""TC Inbox (/dashboard/agent/inbox/). Everyone on the agent pages may view; only admins
decide (Confirm / Ignore / Reject), by POST. Non-admins see only items whose proposed
schedule belongs to one of their clients."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from core.models import Schedule
from core.views import _account_qs

from .confirm import ConfirmRefused, confirm_upload, decide, eligible_schedules, label
from .models import REASONS, STATUS, InboundAttachment

AGENT_ROLES = ['super_admin', 'admin', 'team_head', 'planner', 'operations']
ADMIN_ROLES = ['super_admin', 'admin']
REASON_LABEL = dict(REASONS)
STATUS_LABEL = dict(STATUS)
TONE = {'suggested': 'agent', 'needs_review': 'warn', 'uploaded': 'ok', 'ignored': 'neutral',
        'duplicate': 'neutral', 'rejected': 'bad', 'new': 'info', 'processing': 'info'}


def _visible(user):
    qs = InboundAttachment.objects.select_related('email', 'suggested_schedule', 'suggested_schedule__account',
                                                  'tc_report', 'duplicate_of')
    if user.role not in ADMIN_ROLES:
        qs = qs.filter(suggested_schedule__account_id__in=_account_qs(user).values('id'))
    return qs


@login_required
@role_required(AGENT_ROLES)
def inbox(request):
    qs = _visible(request.user)
    status, reason = request.GET.get('status', ''), request.GET.get('reason', '')
    if status and status in STATUS_LABEL:
        qs = qs.filter(status=status)
    if reason and reason in REASON_LABEL:            # REASONS contains '' ("—"): never filter on it
        qs = qs.filter(reason=reason)
    page = Paginator(qs.order_by('-id'), 40).get_page(request.GET.get('page'))
    for a in page.object_list:
        a.tone, a.status_label = TONE.get(a.status, 'neutral'), STATUS_LABEL.get(a.status)
        a.reason_label = REASON_LABEL.get(a.reason, '') if a.reason else ''
    from django.db.models import Count
    counts = dict(_visible(request.user).order_by().values_list('status').annotate(n=Count('id')))
    return render(request, 'agent/inbox.html', {
        'page': page, 'status': status, 'reason': reason,
        'statuses': [(k, lbl, counts[k]) for k, lbl in STATUS if k in counts], 'reasons': [r for r in REASONS if r[0]],
        'is_admin': request.user.role in ADMIN_ROLES})


@login_required
@role_required(AGENT_ROLES)
def inbox_detail(request, pk):
    att = _visible(request.user).filter(pk=pk).first()
    if att is None:
        raise Http404
    is_admin = request.user.role in ADMIN_ROLES
    ev = att.evidence or {}
    allowed = set(_account_qs(request.user).values_list('id', flat=True))
    cands = []
    for sid, v in (ev.get('candidates') or {}).items():
        sch = v.get('schedule') or {}
        s = Schedule.objects.filter(pk=sid).first()
        if s is None or (not is_admin and s.account_id not in allowed):
            continue
        cands.append({'id': int(sid), 'label': label(s),
                      'info': {'start_date': '', 'window_end': '', **sch},
                      'overlap': {'total': '', 'candidate': '', 'foreign': '', 'overlap': '', **(v.get('overlap') or {})},
                      'fails': v.get('fails') or []})
    choices = [(s.id, label(s)) for s in eligible_schedules(att)] if is_admin else []
    rule = {'decision': '', 'reason': '', 'schedule_id': None, **(att.rule_verdict or {})}
    llm = {'error': '', 'decision': '', 'reason_code': '', 'schedule_id': None, 'note': '',
           'suspicious_instruction': False, **(att.llm_verdict or {})}
    detect = {'ok': False, 'error': '', 'file_type': '', 'pdf': None, 'channel_guess': '', 'date_min': '',
              'date_max': '', 'row_count': 0, 'skipped_rows': 0, 'missing_columns': [], 'distinct_themes': 0,
              'sample_themes': [], **(ev.get('detect') or {})}
    detect['present'] = bool(ev.get('detect'))
    return render(request, 'agent/inbox_detail.html', {
        'a': att, 'is_admin': is_admin, 'detect': detect, 'cands': cands, 'rule': rule, 'llm': llm,
        'has_llm': bool(att.llm_verdict), 'why': (ev.get('final') or {}).get('why', ''),
        'rules_only_why': ev.get('rules_only_why', ''), 'choices': choices,
        'status_label': STATUS_LABEL.get(att.status),
        'reason_label': REASON_LABEL.get(att.reason, '') if att.reason else '',
        'tone': TONE.get(att.status, 'neutral'),
        'can_decide': is_admin and att.status in ('new', 'needs_review', 'suggested') and not att.tc_report_id,
    })


@login_required
@role_required(ADMIN_ROLES)
@require_POST
def inbox_decide(request, pk):
    att = get_object_or_404(InboundAttachment, pk=pk)
    action = request.POST.get('action')
    if action == 'confirm':
        s = Schedule.objects.filter(pk=request.POST.get('schedule_id')).select_related('account').first()
        if s is None or s.id not in {x.id for x in eligible_schedules(att)}:
            messages.error(request, 'Choose one of the listed schedules.')
            return redirect(f'/dashboard/agent/inbox/{att.id}/')
        try:
            res = confirm_upload(att, s, request.user, dates_ack=request.POST.get('dates_ack') == '1')
        except ConfirmRefused as exc:
            messages.error(request, f'Not uploaded — {REASON_LABEL.get(exc.reason, exc.reason)}. {exc.detail}')
            return redirect(f'/dashboard/agent/inbox/{att.id}/')
        messages.success(request, f"Uploaded {res['rows']} TC rows to #{s.schedule_number} "
                                  f"({res['channel']} · {res['month']}). The scope is queued for its next run.")
    elif action in ('ignore', 'reject') and (att.tc_report_id or att.status not in ('new', 'needs_review', 'suggested')):
        messages.error(request, f'Not changed — this item is already {att.status}.')
    elif action in ('ignore', 'reject'):
        decide(att, request.user, 'ignored' if action == 'ignore' else 'rejected', request.POST.get('note', ''))
        messages.success(request, 'Saved.')
    return redirect(f'/dashboard/agent/inbox/{att.id}/')
