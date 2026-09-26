"""Daily digest (Phase 3 g; owner Q3, Q5). Admins only; never channels or clients.

maybe_send(now): the first agent_cycle at or after AgentConfig.digest_time (Asia/Colombo)
sends one digest per recipient per day, deduplicated by NotificationLog
(dedupe_key 'digest|<date>|<user id>').
  recipients  AgentConfig.digest_recipients (active super_admin/admin only); empty = every
              active super_admin/admin. The agent service user never receives it.
  transport   the core SMTP settings through get_setting (email_enabled, email_host, ...);
              when email is off or not configured the digest is only logged
              (NotificationLog via='log', AgentRun(kind='digest') keeps the text).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.utils import timezone

from core.models import Schedule, get_setting, get_setting_int

from .heartbeat import health
from .models import AgentConfig, AgentRun, FindingLedger, NotificationLog, PendingEffect, ScopeState

log = logging.getLogger('agent.digest')

ADMIN_ROLES = ('super_admin', 'admin')
TOP_EFFECTS = 20                    # Q5
EFFECT_MIN = 1                      # Q5: |Δ| >= 1 on Aired or Missed
STALE_INPUT_DAYS = 7
FLAPPING_REOPENS = 2                # S3
TEMPLATE_KEY = 'daily_digest'


def recipients(cfg) -> list:
    from .service import service_email
    User = get_user_model()
    chosen = cfg.digest_recipients.filter(is_active=True, role__in=ADMIN_ROLES) if cfg.pk else User.objects.none()
    qs = chosen if chosen.exists() else User.objects.filter(is_active=True, role__in=ADMIN_ROLES)
    return list(qs.exclude(email__iexact=service_email()).order_by('id'))


def _since(now):
    last = AgentRun.objects.filter(kind='digest', status='ok').order_by('-started_at').first()
    return last.started_at if last else now - timedelta(days=1)


def _effect_rows(since) -> tuple[list, int]:
    latest = {}
    for pe in (PendingEffect.objects.filter(created_at__gte=since, max_abs__gte=EFFECT_MIN)
               .select_related('scope__account', 'schedule').order_by('-created_at', '-id')):
        latest.setdefault((pe.scope_id, pe.schedule_id), pe)
    rows = []
    for pe in latest.values():
        for b in pe.by_brand:
            d = b.get('deltas', {})
            size = max(abs(d.get('aired', 0)), abs(d.get('missed', 0)))
            if size >= EFFECT_MIN:
                rows.append({'account': pe.scope.account.name, 'channel': pe.scope.channel,
                             'month': pe.scope.month,
                             'schedule_number': pe.schedule.schedule_number if pe.schedule else '',
                             'section': b.get('section'), 'product': b.get('product'), 'dur': b.get('dur'),
                             'aired': d.get('aired', 0), 'missed': d.get('missed', 0), 'size': size})
    rows.sort(key=lambda r: (-r['size'], r['account'], r['channel'], r['product']))
    return rows[:TOP_EFFECTS], max(0, len(rows) - TOP_EFFECTS)


def _stale_inputs(today) -> list:
    out = []
    for sc in ScopeState.objects.filter(state='WAITING_INPUTS').select_related('account'):
        end = (Schedule.objects.filter(account_id=sc.account_id, channel=sc.channel, month=sc.month,
                                       is_superseded=False).order_by('-end_date').values_list('end_date', flat=True)
               .first())
        if end and (today - end).days > STALE_INPUT_DAYS:
            out.append({'account': sc.account.name, 'channel': sc.channel, 'month': sc.month,
                        'reason': sc.reason, 'days': (today - end).days})
    return sorted(out, key=lambda r: -r['days'])


def build(now) -> dict:
    since = _since(now)
    today = timezone.localtime(now).date()
    effects, more = _effect_rows(since)
    new = (FindingLedger.objects.filter(first_seen__gte=since).select_related('scope__account')
           .order_by('code', 'scope_id'))
    window = AgentRun.objects.filter(kind='shadow_window', status='ok').order_by('-started_at').first()
    return {
        'date': today.isoformat(), 'since': since,
        'states': sorted(Counter(ScopeState.objects.values_list('state', flat=True)).items()),
        'new_findings': [{'code': f.code, 'account': f.scope.account.name, 'channel': f.scope.channel,
                          'month': f.scope.month, 'brand': f.brand, 'duration': f.duration,
                          'actionable': f.actionable, 'text': f.text} for f in new[:50]],
        'new_findings_total': new.count(),
        'flapping': [{'code': f.code, 'account': f.scope.account.name, 'channel': f.scope.channel,
                      'month': f.scope.month, 'brand': f.brand, 'reopen_count': f.reopen_count}
                     for f in FindingLedger.objects.filter(open=True, reopen_count__gte=FLAPPING_REOPENS)
                     .select_related('scope__account')],
        'stale_inputs': _stale_inputs(today),
        'health': [h for h in health(now) if h['state'] != 'ok'],
        'service_user_missing': service_user_missing(),
        'effects': effects, 'effects_more': more,
        'v5_unexplained': v5_open(now),
        'window': ({'night': window.detail.get('night'), 'used_seconds': window.detail.get('used_seconds'),
                    'dry_runs': window.detail.get('dry_runs'),
                    'changed_tables': sorted((window.detail.get('diff') or {}).keys()),
                    'agent_actions': window.detail.get('agent_actions_in_window')} if window else None),
    }


def service_user_missing() -> list:
    """T8: accounts the agent service user cannot see, as the last cycle recorded them."""
    from .models import Heartbeat
    hb = Heartbeat.objects.filter(name='agent_cycle').first()
    return list(((hb.counts if hb else None) or {}).get('service_user_missing_accounts') or [])


def v5_open(now) -> list:
    """T1: every open V5_UNEXPLAINED row with its age (they persist until acknowledged)."""
    from .measure import v5_criterion
    return v5_criterion(now=now)['open_rows']


def _smtp():
    if not get_setting_int('email_enabled', 0):
        return None
    host = get_setting('email_host', '').strip()
    user = get_setting('email_host_user', '').strip()
    sender = get_setting('email_from_address', '').strip() or user
    if not host or not user or not sender:
        return None
    return {'host': host, 'port': get_setting_int('email_port', 587),
            'use_tls': bool(get_setting_int('email_use_tls', 1)), 'username': user,
            'password': get_setting('email_host_password', '').strip(), 'from': sender}


def maybe_send(now=None) -> dict:
    now = now or timezone.now()
    cfg = AgentConfig.objects.filter(pk=1).first() or AgentConfig()
    local = timezone.localtime(now)
    if local.time() < cfg.digest_time:
        return {'sent': 0, 'reason': 'before_digest_time'}
    day = local.date().isoformat()
    todo = [u for u in recipients(cfg)
            if not NotificationLog.objects.filter(dedupe_key=f'digest|{day}|{u.id}').exists()]
    if not todo:
        return {'sent': 0, 'reason': 'already_sent_or_no_recipients'}
    ctx = build(now)
    text = render_to_string('agent/_digest_email.txt', ctx)
    html = render_to_string('agent/_digest_email.html', ctx)
    subject = f'Reconciliation Agent daily digest — {day}'
    smtp = _smtp()
    conn = None
    if smtp:
        from django.core.mail import get_connection
        conn = get_connection(
            backend=getattr(settings, 'AGENT_DIGEST_EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend'),
            host=smtp['host'], port=smtp['port'], username=smtp['username'], password=smtp['password'],
            use_tls=smtp['use_tls'], fail_silently=False)
    out = {'sent': 0, 'logged': 0, 'failed': 0}
    for u in todo:
        status, via = 'logged', 'log'
        if conn is not None and u.email:
            from django.core.mail import EmailMultiAlternatives
            msg = EmailMultiAlternatives(subject, text, smtp['from'], [u.email], connection=conn)
            msg.attach_alternative(html, 'text/html')
            try:
                msg.send()
                status, via = 'sent', 'email'
            except Exception as exc:              # noqa: BLE001 — recorded; no retry the same day
                log.error('digest to %s failed: %s', u.email, exc)
                status, via = 'failed', 'email'
        else:
            log.info('digest (email off) for %s:\n%s', u.email, text)
        NotificationLog.objects.create(dedupe_key=f'digest|{day}|{u.id}', template_key=TEMPLATE_KEY,
                                       recipient=u, via=via, status=status)
        out[status if status != 'sent' else 'sent'] += 1
    AgentRun.objects.create(kind='digest', status='ok', finished_at=timezone.now(), detail={
        'date': day, 'recipients': [u.id for u in todo], **out, 'text': text[:20000]})
    return out
