"""Reconciliation Agent console (Phase 3.2, autonomy level 0). Read-only helpers.

card(user)          the sidebar agent card: status, last cycle, next expected cycle, heartbeat health
schedule_rows(...)  latest OBSERVED SummarySnapshot per schedule (no new Summary calculation)
report_rows(...)    per scope: sign-off state + totals from observed snapshots
resolve_theme(...)  theme tester: TC theme -> brands (engine resolver), LMRB theme -> brands
                    (engine map + the engines' exact / '*' prefix rule). Writes nothing.

There are no approve / apply controls anywhere in the console (level 0).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.utils import timezone

from core.models import Account, SummaryReportMeta
from verification.tc_engine import (
    _brands_for_tc_theme, _build_lmrb_theme_map, _build_reverse_tc_theme_map, _lmrb_themes_for_brand,
)

from .heartbeat import health
from .models import AgentConfig, AgentRun, ScopeState, SummarySnapshot

log = logging.getLogger('agent.console')

CRON_MINUTES = 15            # railway/cron-agent.json: */15 * * * *
STAFF_ROLES = ('super_admin', 'admin', 'team_head', 'planner', 'operations')
ADMIN_ROLES = ('super_admin', 'admin')


def next_tick(now):
    """Next */15 cron tick after `now`."""
    base = now.replace(second=0, microsecond=0)
    return base + timedelta(minutes=CRON_MINUTES - base.minute % CRON_MINUTES)


def card(user) -> dict | None:
    """Context for the sidebar card; None for channel officers and anonymous users (they never see
    it). It runs on every core page, so it must never break one: any error (for example the agent
    tables not migrated yet) is logged and the card is simply not shown."""
    try:
        return _card(user)
    except Exception:                                  # noqa: BLE001 — never break a core page
        log.exception('agent card failed; not shown')
        return None


def _card(user) -> dict | None:
    if not getattr(user, 'is_authenticated', False) or getattr(user, 'role', None) not in STAFF_ROLES:
        return None
    cfg = AgentConfig.objects.filter(pk=1).first() or AgentConfig()        # read only: never creates it
    last = AgentRun.objects.filter(kind='cycle').order_by('-started_at').first()
    rows = health()
    cycle_row = next((h for h in rows if h['name'] == 'agent_cycle'), None)
    bad = [h for h in rows if h['state'] not in ('ok', 'never')]
    now = timezone.now()
    status = 'running' if cfg.enabled else 'paused'
    return {
        'status': status, 'enabled': cfg.enabled, 'level': cfg.autonomy_level,
        'last_cycle_at': last.started_at if last else None,
        'last_cycle_outcome': ((last.detail or {}).get('outcome') or last.status) if last else '',
        'next_cycle_at': next_tick(now) if cfg.enabled else None,
        'run_requested_at': cfg.run_requested_at,
        'health_state': 'ok' if not bad else ('alert' if any(h['state'] in ('alert', 'error') for h in bad) else 'warn'),
        'health_note': (bad[0]['label'] + ': ' + (bad[0]['note'] or bad[0]['state'])) if bad else 'All jobs ok',
        'cycle_state': cycle_row['state'] if cycle_row else 'never',
        'can_control': getattr(user, 'role', None) in ADMIN_ROLES,
    }


def summary_url(account_id, channel, month, schedule_id=None) -> str:
    q = {'account_id': account_id, 'channel': channel, 'month': month}
    if schedule_id:
        q['schedule_id'] = schedule_id
    return '/dashboard/summary/?' + urlencode(q)


def _latest_observed(account_ids, month=''):
    """Latest observed snapshot per ACTIVE schedule (Rule 12: superseded versions and deleted
    schedules are left out), for the user's accounts only."""
    from verification.engine import active_schedule_ids
    scopes = ScopeState.objects.filter(account_id__in=account_ids).select_related('account')
    if month:
        scopes = scopes.filter(month=month)
    out = []
    for sc in scopes:
        for sid in active_schedule_ids(sc.account_id, sc.channel, sc.month):
            snap = (SummarySnapshot.objects.filter(kind='observed', scope=sc, schedule_id=sid)
                    .select_related('scope__account').order_by('-created_at', '-id').first())
            if snap is not None:
                out.append(snap)
    return out


def _totals(data: dict) -> dict:
    c, s = data.get('commercial_total') or {}, data.get('sponsorship_total') or {}
    keys = ('planned', 'aired', 'third_party', 'extra', 'missed')
    return {k: int(c.get(k) or 0) + int(s.get(k) or 0) for k in keys}


def schedule_rows(account_ids, month='') -> list[dict]:
    out = []
    for snap in _latest_observed(account_ids, month):
        sc = snap.scope
        t = _totals(snap.data or {})
        out.append({'account': sc.account.name, 'channel': sc.channel, 'month': sc.month,
                    'schedule_number': snap.schedule_number, 'schedule_id': snap.schedule_id,
                    'state': sc.state, 'reason': sc.reason, 'observed_at': snap.created_at, **t,
                    'summary_url': summary_url(sc.account_id, sc.channel, sc.month, snap.schedule_id)})
    return sorted(out, key=lambda r: (r['account'], r['channel'], r['month'], r['schedule_number']))


def report_rows(account_ids, month='') -> list[dict]:
    by_scope = {}
    for snap in _latest_observed(account_ids, month):
        by_scope.setdefault(snap.scope_id, []).append(snap)
    metas = {(m.account_id, m.channel, m.month): m for m in
             SummaryReportMeta.objects.filter(account_id__in=account_ids)}
    out = []
    for sid, snaps in by_scope.items():
        sc = snaps[0].scope
        meta = metas.get((sc.account_id, sc.channel, sc.month))
        totals = {k: sum(_totals(s.data or {})[k] for s in snaps) for k in ('planned', 'aired', 'missed')}
        # Sign-off comes from the agent's single source of scope state (agent/readiness.py).
        if sc.state == 'AUTHORISED':
            who = meta.authorised_by.strip() if meta and meta.authorised_by.strip() else ''
            signoff = f'Authorised by {who}' if who else 'Authorised'
        elif sc.state == 'READY_FOR_SIGNOFF':
            signoff = 'Ready for sign-off'
        else:
            signoff = 'Not ready'
        q = urlencode({'account_id': sc.account_id, 'channel': sc.channel, 'month': sc.month})
        out.append({'account': sc.account.name, 'channel': sc.channel, 'month': sc.month, 'state': sc.state,
                    'signoff': signoff, 'schedules': len(snaps), **totals,
                    'observed_at': max(s.created_at for s in snaps),
                    'prepared_by': meta.prepared_by if meta else '', 'checked_by': meta.checked_by if meta else '',
                    'summary_url': f'/dashboard/summary/?{q}', 'pdf_url': f'/dashboard/summary/pdf/?{q}',
                    'excel_url': f'/dashboard/summary/excel/?{q}'})
    return sorted(out, key=lambda r: (r['account'], r['channel'], r['month']))


def _norm(s) -> str:
    return str(s).lower().strip() if s else ''


def resolve_theme(account_id, kind: str, theme: str, duration) -> dict:
    """Which brand(s) a TC or LMRB theme resolves to for an account, with the engine resolvers.
    kind='tc': _build_reverse_tc_theme_map + _brands_for_tc_theme (as reconcile_tc).
    kind='lmrb': _build_lmrb_theme_map + _lmrb_themes_for_brand per brand, matched with the engines'
    rule (exact after lower/strip, or a stored '*' prefix)."""
    dur = int(duration) if duration not in (None, '') else None
    if kind == 'tc':
        brands = _brands_for_tc_theme(theme, dur, _build_reverse_tc_theme_map(account_id))
        return {'brands': [{'brand': b, 'via': 'BrandMapping.tc_theme', 'product': ''} for b in brands]}
    nt, out = _norm(theme), []
    lmap = _build_lmrb_theme_map(account_id)
    for brand in sorted(lmap):
        for stored, product in _lmrb_themes_for_brand(brand, dur, lmap):
            if stored == nt or (stored.endswith('*') and nt.startswith(stored[:-1])):
                out.append({'brand': brand, 'via': f'BrandMapping.theme "{stored}"', 'product': product})
                break
    return {'brands': out}

