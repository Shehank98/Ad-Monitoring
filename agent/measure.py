"""Diagnosis quality (Phase 3 S2). Pure reads of agent tables.

precision(): from labels only (S2d). precision = correct / (correct + incorrect), per code and
    overall, with n. 'unsure' labels are counted but never enter the ratio. Owner labels
    (agent_label_scopes) and feedback labels (queue buttons) are reported separately and
    together.
inferred(): display only, marked "inferred", never used for an exit criterion (S2a).
    action alignment = closed findings whose disappearance matched a human change /
                       all closed findings
    action coverage  = observations with a human change where an agent finding was already
                       open on that scope / all observations with a human change
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from .ledger import HUMAN_RESOLUTIONS, OWNER_ONLY, UNMAPPED_BRAND
from .models import AgentRun, FindingLedger

WINDOW_DAYS = 30


def _ratio(c, i):
    return round(c / (c + i), 3) if (c + i) else None


def precision(source: str | None = None) -> dict:
    """{'codes': [{code, correct, incorrect, unsure, n, precision}], 'overall': {...},
    'unmapped_brand': {...}}. `source`: 'owner', 'feedback' or None (both)."""
    qs = FindingLedger.objects.exclude(label='').exclude(resolution=OWNER_ONLY)
    if source:
        qs = qs.filter(label_source=source)
    per = defaultdict(lambda: {'correct': 0, 'incorrect': 0, 'unsure': 0})
    for code, label in qs.values_list('code', 'label'):
        if label in ('correct', 'incorrect', 'unsure'):
            per[code][label] += 1
    rows, tot, um = [], {'correct': 0, 'incorrect': 0, 'unsure': 0}, {'correct': 0, 'incorrect': 0, 'unsure': 0}
    for code in sorted(per):
        d = per[code]
        rows.append({'code': code, **d, 'n': d['correct'] + d['incorrect'],
                     'precision': _ratio(d['correct'], d['incorrect'])})
        for k in tot:
            tot[k] += d[k]
            if code in UNMAPPED_BRAND:
                um[k] += d[k]
    return {'codes': rows,
            'overall': {**tot, 'n': tot['correct'] + tot['incorrect'],
                        'precision': _ratio(tot['correct'], tot['incorrect']),
                        'codes_labelled': sum(1 for r in rows if r['n'])},
            'unmapped_brand': {**um, 'n': um['correct'] + um['incorrect'],
                               'precision': _ratio(um['correct'], um['incorrect'])}}


def misses() -> dict:
    """Owner labels the agent has not found (per code)."""
    out = defaultdict(int)
    for code in FindingLedger.objects.filter(resolution=OWNER_ONLY).values_list('code', flat=True):
        out[code] += 1
    return dict(sorted(out.items()))


def inferred(days: int = WINDOW_DAYS, now=None) -> dict:
    now = now or timezone.now()
    since = now - timedelta(days=days)
    closed = FindingLedger.objects.filter(open=False, resolved_at__gte=since).exclude(resolution=OWNER_ONLY)
    per = defaultdict(lambda: {'closed': 0, 'aligned': 0})
    for code, res in closed.values_list('code', 'resolution'):
        per[code]['closed'] += 1
        per[code]['aligned'] += res in HUMAN_RESOLUTIONS
    with_change = covered = 0
    for d in AgentRun.objects.filter(kind='scope', status='ok', started_at__gte=since).values_list('detail', flat=True):
        fs = (d or {}).get('findings') or {}
        if fs.get('human_changes'):
            with_change += 1
            covered += bool(fs.get('open_before'))
    rows = [{'code': c, **v, 'alignment': round(v['aligned'] / v['closed'], 3) if v['closed'] else None}
            for c, v in sorted(per.items())]
    n_closed = sum(v['closed'] for v in per.values())
    return {'label': 'inferred', 'days': days, 'codes': rows,
            'alignment': round(sum(v['aligned'] for v in per.values()) / n_closed, 3) if n_closed else None,
            'closed': n_closed, 'observations_with_human_change': with_change,
            'coverage': round(covered / with_change, 3) if with_change else None}


def quality() -> dict:
    """Everything the Overview 'Diagnosis quality' card shows."""
    return {'labelled': precision(), 'owner': precision('owner'), 'feedback': precision('feedback'),
            'misses': misses(), 'inferred': inferred(),
            'open': FindingLedger.objects.filter(open=True).count(),
            'open_actionable': FindingLedger.objects.filter(open=True, actionable=True).count(),
            'flapping': FindingLedger.objects.filter(open=True, reopen_count__gte=2).count()}


def cycle_health(days: int = 14, now=None) -> dict:
    """Exit criteria 4, 6 and 7, over the last `days` days (agent tables only).
    yield_share   (busy_yielded + timeout_yielded) / scope visits, observe and shadow steps
    p95_seconds   95th percentile of cycle duration
    nights_ok     closed shadow windows in which no scope was skipped for budget_exhausted
    """
    now = now or timezone.now()
    since = now - timedelta(days=days)
    durations = sorted(float((d or {}).get('duration_seconds', 0)) for d in
                       AgentRun.objects.filter(kind='cycle', status='ok', started_at__gte=since)
                       .values_list('detail', flat=True))
    visits = yields = 0
    budget_nights = set()
    for started, d in (AgentRun.objects.filter(kind='scope', started_at__gte=since, detail__mode='observe')
                       .values_list('started_at', 'detail')):
        d = d or {}
        if d.get('outcome') == 'debounced':
            continue
        visits += 1
        yields += d.get('outcome') in ('busy_yielded', 'timeout_yielded')
        sh = d.get('shadow') or {}
        if sh:
            visits += 1
            yields += sh.get('outcome') in ('busy_yielded', 'timeout_yielded')
            if sh.get('reason') == 'budget_exhausted':
                budget_nights.add(timezone.localtime(started).date())
    windows = list(AgentRun.objects.filter(kind='shadow_window', status='ok', started_at__gte=since)
                   .values_list('detail', flat=True))
    nights = len(windows)
    nights_ok = sum(1 for d in windows if (d or {}).get('night') and
                    not any(n.isoformat() in (d['night'], _next(d['night'])) for n in budget_nights))
    last = AgentRun.objects.filter(kind='cycle').order_by('-started_at').first()
    return {'days': days, 'cycles': len(durations),
            'p95_seconds': durations[max(0, int(round(0.95 * len(durations))) - 1)] if durations else None,
            'max_seconds': durations[-1] if durations else None,
            'visits': visits, 'yields': yields,
            'yield_share': round(yields / visits, 4) if visits else None,
            'nights': nights, 'nights_ok': nights_ok,
            'nights_ok_share': round(nights_ok / nights, 3) if nights else None,
            'agent_actions_in_windows': sum(int((d or {}).get('agent_actions_in_window') or 0) for d in windows),
            'last_cycle': last}


def _next(iso):
    import datetime
    return (datetime.date.fromisoformat(iso) + timedelta(days=1)).isoformat()
