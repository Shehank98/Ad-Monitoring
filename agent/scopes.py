"""
Read-only scope view-model for the Reconciliation Agent preview pages.

A scope is (account, channel, month): the unit the engines, the LMRB pool and
SummaryReportMeta work on (AGENT_BUILD_BRIEF.md §5). The STATE comes from
agent/readiness.py (the single source of scope state); this module only shapes it
for the templates and adds display findings.

Rules this module follows:
- It never writes. Every function only reads core models and calls read-only
  helpers (active_schedule_ids, build_summary_data, tc theme maps).
- Channel and month strings are used exactly as stored on Schedule (R2).
- Active schedules come from the engine's own Rule 12 helper (brief §5).
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime



from core.models import MatchResult, Schedule, ScheduleRow, TransmissionReport
from verification.tc_engine import _build_tc_theme_map

from .models import ScopeState
from .readiness import REASON_LABEL, assess

# Ordered as the pipeline reads left to right.
STATES = [
    ('STILL_AIRING',      'Still airing',       'neutral'),
    ('WAITING_INPUTS',    'Waiting for inputs', 'warn'),
    ('STANDALONE',        'Standalone TC',      'neutral'),
    ('NEEDS_HUMAN',       'Needs a person',     'bad'),
    ('MAPPING',           'Needs mapping',      'bad'),
    ('RECONCILING',       'Not reconciled',     'agent'),
    ('READY_FOR_SIGNOFF', 'Ready for sign-off', 'info'),
    ('AUTHORISED',        'Authorised',         'ok'),
]
STATE_LABEL = {k: label for k, label, _ in STATES}
STATE_TONE = {k: tone for k, _, tone in STATES}


COMMERCIAL = 'COMMERCIAL BENEFITS'


def parse_month(month: str):
    """'January 2025' -> date(2025, 1, 1), or None."""
    try:
        return datetime.strptime(month.strip(), '%B %Y').date()
    except (ValueError, AttributeError):
        return None


def month_days(month: str) -> int:
    d = parse_month(month)
    return calendar.monthrange(d.year, d.month)[1] if d else 31


def available_months(account_ids) -> list[str]:
    """Distinct Schedule months for these accounts, newest first."""
    months = set(
        Schedule.objects.filter(account_id__in=account_ids)
        .order_by().values_list('month', flat=True).distinct()
    )
    return sorted(months, key=lambda m: parse_month(m) or date.min, reverse=True)


@dataclass
class Finding:
    code: str
    text: str
    tone: str = 'warn'
    link: str = ''
    link_label: str = ''


@dataclass
class ScheduleStatus:
    schedule: Schedule
    has_tc: bool
    rows: int
    matched: int
    pending: int


@dataclass
class Scope:
    account_id: int
    account_name: str
    channel: str
    month: str
    state: str = 'READY_FOR_SIGNOFF'
    reason: str = ''
    start: date | None = None
    end: date | None = None
    lmrb_until: date | None = None
    planned: int = 0
    schedules: list = field(default_factory=list)      # list[ScheduleStatus]
    unmapped: list = field(default_factory=list)       # list[(brand, duration)]
    findings: list = field(default_factory=list)       # list[Finding]
    authorised_by: str = ''

    @property
    def label(self):
        return STATE_LABEL[self.state]

    @property
    def tone(self):
        return STATE_TONE[self.state]

    @property
    def reason_label(self):
        return REASON_LABEL.get(self.reason, self.reason)

    @property
    def schedule_numbers(self):
        return [s.schedule.schedule_number for s in self.schedules]


def _scope_keys(account_ids, month):
    return (
        Schedule.objects.filter(account_id__in=account_ids, month=month)
        .values_list('account_id', 'account__name', 'channel', 'month')
        .distinct()
        .order_by('account__name', 'channel')
    )


def build_scope(account_id, account_name, channel, month, today=None,
                tc_theme_map=None) -> Scope:
    """Shape readiness.assess() for the templates. Read-only: the ScopeState used here
    is never saved."""
    sc = Scope(account_id, account_name, channel, month)
    r = assess(ScopeState(account_id=account_id, channel=channel, month=month),
               today=today, tc_theme_map=tc_theme_map)
    sc.state, sc.reason = r.state, r.reason
    sc.start, sc.end, sc.lmrb_until = r.start, r.end, r.lmrb_until
    sc.authorised_by = r.authorised_by
    sc.unmapped = list(r.unmapped)
    sc.schedules = [ScheduleStatus(schedule=x.schedule, has_tc=x.has_tc, rows=x.rows,
                                   matched=x.matched, pending=x.pending) for x in r.schedules]
    sc.planned = sum(x.rows for x in r.schedules)

    # ── Display findings ──
    all_count = Schedule.objects.filter(account_id=account_id, channel=channel, month=month).count()
    if all_count > len(sc.schedules):
        sc.findings.append(Finding(
            'superseded_present',
            f'{all_count - len(sc.schedules)} older schedule version(s) exist in this scope. '
            'Summary figures are shown per active schedule so they are not double-counted.',
            'info'))
    for brand, dur in r.wildcard_only:
        sc.findings.append(Finding(
            'wildcard_only',
            f'{brand} ({dur}s) is mapped only by a wildcard TC theme. The commercial summary '
            'does not expand wildcards, so Aired will read 0 (discrepancy D3).',
            'warn', '/dashboard/brand-mappings/', 'Brand mappings'))
    for code, text in r.warnings:
        sc.findings.append(Finding(code.lower(), text, 'info'))
    if r.reason == 'no_tc' and TransmissionReport.objects.filter(
            account_id=account_id, channel__iexact=channel, month=month).exclude(channel=channel).exists():
        # Detection only: the stored strings are shown, never used as values.
        sc.findings.append(Finding(
            'channel_case',
            'A TC exists for this channel with different capitalisation. '
            'The summary uses exact channel strings, so it would not be counted.',
            'bad', '/dashboard/tc/', 'TC reports'))
    return sc


def build_scopes(account_ids, month, today=None) -> list[Scope]:
    maps = {}
    out = []
    for acc_id, acc_name, channel, mo in _scope_keys(account_ids, month):
        if acc_id not in maps:
            maps[acc_id] = _build_tc_theme_map(acc_id)
        out.append(build_scope(acc_id, acc_name, channel, mo, today, maps[acc_id]))
    return out


def state_counts(scopes) -> list[dict]:
    total = len(scopes) or 1
    rows = []
    for key, label, tone in STATES:
        n = sum(1 for s in scopes if s.state == key)
        rows.append({'key': key, 'label': label, 'tone': tone, 'count': n, 'pct': n * 100 / total})
    return rows


# ── Spot strip (one track per brand × duration) ──────────────────────────────

RESULT_TONE = {
    'matched': 'ok', 'manual_match': 'info', 'programme_mismatch': 'warn',
    'late_telecast': 'warn', 'not_aired': 'bad', 'no_mapping': 'bad', 'pending': 'none',
}
RESULT_LABEL = {
    'matched': 'Matched', 'manual_match': 'Manual match', 'programme_mismatch': 'Programme mismatch',
    'late_telecast': 'Late telecast', 'not_aired': 'Not aired', 'no_mapping': 'No brand mapping',
    'pending': 'Pending',
}


def _secs(t: str) -> int:
    try:
        parts = [int(float(p)) for p in str(t).split(':')]
        while len(parts) < 3:
            parts.append(0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except (ValueError, TypeError):
        return 0


def spot_strip(scope: Scope, limit_rows: int = 1500) -> dict:
    """Ticks for commercial ScheduleRows of the active schedules, coloured by the
    latest MatchResult status (Schedule↔LMRB engine). Read-only."""
    ids = [st.schedule.id for st in scope.schedules]
    rows = list(
        ScheduleRow.objects.filter(schedule_id__in=ids, ad_type=COMMERCIAL)
        .order_by('brand', 'duration', 'date', 'start_time')
        .values('id', 'brand', 'duration', 'date', 'start_time', 'is_manual_matched')[:limit_rows]
    )
    latest = {}
    for sr_id, status in (MatchResult.objects.filter(schedule_row_id__in=[r['id'] for r in rows])
                          .order_by('schedule_row_id', '-run_at')
                          .values_list('schedule_row_id', 'status')):
        latest.setdefault(sr_id, status)
    days = month_days(scope.month)
    first = parse_month(scope.month)
    tracks, order, counts = {}, [], {}
    for r in rows:
        key = (r['brand'], r['duration'])
        if key not in tracks:
            tracks[key] = []
            order.append(key)
        status = 'manual_match' if r['is_manual_matched'] else latest.get(r['id'], 'pending')
        counts[status] = counts.get(status, 0) + 1
        if r['date'] and first and (r['date'].year, r['date'].month) == (first.year, first.month):
            day = r['date'].day
        elif r['date'] and first:
            day = days if r['date'] > first else 1
        else:
            day = 1
        left = ((day - 1) + _secs(r['start_time']) / 86400) / days * 100
        tracks[key].append({
            'left': round(min(max(left, 0), 99.6), 2),
            'tone': RESULT_TONE.get(status, 'none'),
            'label': f"{r['brand']} · {r['date']} {r['start_time']} · {RESULT_LABEL.get(status, status)}",
        })
    return {
        'tracks': [{'brand': b, 'dur': d, 'ticks': tracks[(b, d)]} for b, d in order],
        'days': days,
        'axis': [1, 8, 15, 22, days],
        'legend': [{'tone': RESULT_TONE[k], 'label': RESULT_LABEL[k], 'count': counts.get(k, 0)}
                   for k in ('matched', 'programme_mismatch', 'late_telecast', 'manual_match',
                             'not_aired', 'no_mapping', 'pending') if counts.get(k)],
        'truncated': len(rows) >= limit_rows,
    }
