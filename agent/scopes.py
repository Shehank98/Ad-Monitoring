"""
Read-only scope state for the Nova agent UI.

A scope is (account, channel, month): the unit the engines, the LMRB pool and
SummaryReportMeta work on (AGENT_BUILD_BRIEF.md §5). This module derives a
*preview* of the brief's state machine (§7) purely from existing core data.

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

from django.db.models import Max, Min

from core.models import (
    LMRBRow, MatchResult, Schedule, ScheduleRow, SummaryReportMeta,
    TCRow, TransmissionReport,
)
from verification.engine import _lmrb_channel_q, active_schedule_ids
from verification.tc_engine import _build_tc_theme_map, _tc_themes_for_brand

# Ordered as the pipeline reads left to right.
STATES = [
    ('STILL_AIRING',      'Still airing',       'neutral'),
    ('WAITING_INPUTS',    'Waiting for inputs', 'warn'),
    ('MAPPING',           'Needs mapping',      'bad'),
    ('RECONCILING',       'Not reconciled',     'agent'),
    ('READY_FOR_SIGNOFF', 'Ready for sign-off', 'info'),
    ('AUTHORISED',        'Authorised',         'ok'),
]
STATE_LABEL = {k: label for k, label, _ in STATES}
STATE_TONE = {k: tone for k, _, tone in STATES}

REASON_LABEL = {
    'no_lmrb':       'No LMRB data for this channel',
    'lmrb_partial':  'LMRB data stops before the period ends',
    'no_tc':         'TC not received',
    'tc_not_linked': 'TC uploaded but not linked to a schedule',
}

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
        .values_list('month', flat=True).distinct()
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
    """Derive the state of one scope. Read-only."""
    today = today or date.today()
    sc = Scope(account_id, account_name, channel, month)
    active_ids = active_schedule_ids(account_id, channel, month)
    active = list(Schedule.objects.filter(id__in=active_ids))
    active.sort(key=lambda s: active_ids.index(s.id))

    # ── Findings about versions (Rule 12 reads version only; see D5/D6) ──
    all_nums = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month,
    ).count()
    if all_nums > len(active):
        sc.findings.append(Finding(
            'superseded_present',
            f'{all_nums - len(active)} older schedule version(s) exist in this scope. '
            'Summary figures are shown per active schedule so they are not double-counted.',
            'info'))

    # ── Period ──
    starts = [s.start_date for s in active if s.start_date]
    ends = [s.end_date for s in active if s.end_date]
    if not starts or not ends:
        agg = ScheduleRow.objects.filter(schedule_id__in=active_ids).aggregate(a=Min('date'), b=Max('date'))
        starts = starts or ([agg['a']] if agg['a'] else [])
        ends = ends or ([agg['b']] if agg['b'] else [])
    sc.start = min(starts) if starts else None
    sc.end = max(ends) if ends else None

    # ── Per schedule status ──
    for s in active:
        rows = ScheduleRow.objects.filter(schedule=s, ad_type=COMMERCIAL)
        n = rows.count()
        matched = rows.filter(is_matched=True).count() + rows.filter(is_manual_matched=True, is_matched=False).count()
        sc.schedules.append(ScheduleStatus(
            schedule=s,
            has_tc=TransmissionReport.objects.filter(schedule=s).exists(),
            rows=n, matched=matched, pending=n - matched,
        ))
        sc.planned += n

    # ── Mapping (R1: mapping is required evidence) ──
    tc_theme_map = tc_theme_map if tc_theme_map is not None else _build_tc_theme_map(account_id)
    pairs = (ScheduleRow.objects.filter(schedule_id__in=active_ids, ad_type=COMMERCIAL)
             .values_list('brand', 'duration').distinct().order_by('brand', 'duration'))
    wildcard_only = []
    for brand, dur in pairs:
        themes = _tc_themes_for_brand(brand, dur, tc_theme_map)
        if not themes:
            sc.unmapped.append((brand, dur))
        elif all(t.endswith('*') for t in themes):
            wildcard_only.append((brand, dur))
    for brand, dur in wildcard_only:
        sc.findings.append(Finding(
            'wildcard_only',
            f'{brand} ({dur}s) is mapped only by a wildcard TC theme. The commercial summary '
            'does not expand wildcards, so Aired will read 0 (discrepancy D3).',
            'warn', '/dashboard/brand-mappings/', 'Brand mappings'))

    # ── Authorised (frozen for the agent) ──
    meta = SummaryReportMeta.objects.filter(account_id=account_id, channel=channel, month=month).first()
    if meta and meta.authorised_by.strip():
        sc.authorised_by = meta.authorised_by.strip()
        sc.state = 'AUTHORISED'
        return sc

    # ── Still airing ──
    if sc.end and sc.end >= today:
        sc.state = 'STILL_AIRING'
        return sc

    # ── Inputs: L (monitoring) ──
    sc.lmrb_until = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id, source='mediawatch',
    ).aggregate(d=Max('date'))['d']
    if not sc.lmrb_until:
        sc.state, sc.reason = 'WAITING_INPUTS', 'no_lmrb'
        return sc
    if sc.end and sc.lmrb_until < sc.end:
        sc.state, sc.reason = 'WAITING_INPUTS', 'lmrb_partial'
        return sc

    # ── Inputs: T (transmission certificate) ──
    if any(not st.has_tc for st in sc.schedules):
        unlinked = TransmissionReport.objects.filter(
            account_id=account_id, channel=channel, month=month, schedule__isnull=True,
        ).exists()
        sc.state, sc.reason = 'WAITING_INPUTS', ('tc_not_linked' if unlinked else 'no_tc')
        case_only = (not unlinked and TransmissionReport.objects.filter(
            account_id=account_id, channel__iexact=channel, month=month,
        ).exclude(channel=channel).exists())
        if case_only:
            sc.findings.append(Finding(
                'channel_case',
                'A TC exists for this channel with different capitalisation. '
                'The summary uses exact channel strings, so it would not be counted.',
                'bad', '/dashboard/tc/', 'TC reports'))
        return sc

    # ── Mapping ──
    if sc.unmapped:
        sc.state = 'MAPPING'
        return sc

    # ── Reconciled? ──
    tc_rows = TCRow.objects.filter(tc_report__schedule_id__in=active_ids)
    if tc_rows.exists() and not (
        tc_rows.filter(is_schedule_matched=True).exists()
        or tc_rows.filter(is_extra=True).exists()
        or tc_rows.filter(is_lmrb_confirmed=True).exists()
    ):
        sc.state = 'RECONCILING'
        sc.reason = 'TC uploaded but reconciliation has not run'
        return sc

    sc.state = 'READY_FOR_SIGNOFF'
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
