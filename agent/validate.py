"""
Validator V1–V5 (Amendment A3). Checks follow what the code computes
(verification/tc_engine.py build_summary_data, :866-934), NOT CLAUDE.md §9.
There is deliberately no "3rd Party >= Aired" check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.models import (
    ManualMatch, MonitoringData, PeriodSponsorshipMatch, Schedule, SponsorshipLmrbAssignment,
    SystemSetting, TcLmrbMatch, TransmissionReport,
)
from verification.tc_engine import _build_tc_theme_map, _tc_themes_for_brand

from .models import AgentAction, SummarySnapshot


@dataclass
class Check:
    code: str
    ok: bool
    detail: dict = field(default_factory=dict)


def v1_rows(summary: dict, account_id: int, tc_theme_map=None) -> list[Check]:
    """V1 arithmetic consistency for commercial rows."""
    tc_theme_map = tc_theme_map if tc_theme_map is not None else _build_tc_theme_map(account_id)
    out = []
    for row in summary.get('commercial', []):
        pl, ai, tp = row['planned'], row['aired'], row['third_party']
        ex, mi = row['extra'], row['missed']
        mapped = bool(_tc_themes_for_brand(row['product'], row['dur'], tc_theme_map))
        d = {'brand': row['product'], 'dur': row['dur'], 'mapped': mapped}
        if mapped:
            ok = ex == max(0, tp - pl) and mi == max(0, pl - tp) and ai >= tp
            out.append(Check('V1' if ok else 'VALIDATION_FAIL', ok, d))
        else:
            ok = tp == 0 and mi == max(0, pl - ai)
            out.append(Check('NO_TC_MAPPING' if ok else 'VALIDATION_FAIL', ok, d))
    return out


def v1(summary: dict, account_id: int, tc_theme_map=None) -> bool:
    return all(c.ok for c in v1_rows(summary, account_id, tc_theme_map))


def v2(before: int, after: int) -> Check:
    """Lock integrity: an agent run must not increase multi-flag LMRB rows."""
    return Check('V2', after <= before, {'before': before, 'after': after})


def v3(schedules) -> Check:
    """Every TC linked to a schedule has channel and month byte-equal to it."""
    bad = []
    for s in schedules:
        for tr in TransmissionReport.objects.filter(schedule=s):
            if tr.channel.encode() != s.channel.encode() or tr.month.encode() != s.month.encode():
                bad.append({'tc_report_id': tr.id, 'schedule_id': s.id})
    return Check('V3', not bad, {'mismatches': bad})


def v5(scope, schedule, new_sha: str) -> Check:
    """Any change since the last snapshot must be explained by an AgentAction or a new
    upload / match record / settings change for the account. BrandMapping has no
    timestamp, so a mapping edit alone cannot explain a change (reported, not assumed)."""
    last = (SummarySnapshot.objects.filter(scope=scope, schedule=schedule)
            .exclude(kind='golden').order_by('-created_at').first())
    if last is None or last.sha256 == new_sha:
        return Check('V5', True, {'changed': False})
    since = last.created_at
    acc = scope.account_id
    reasons = []
    if AgentAction.objects.filter(scope=scope, created_at__gt=since).exists():
        reasons.append('agent_action')
    for label, model, f in (('schedule_upload', Schedule, 'uploaded_at'),
                            ('monitoring_upload', MonitoringData, 'uploaded_at'),
                            ('tc_upload', TransmissionReport, 'uploaded_at'),
                            ('manual_match', ManualMatch, 'matched_at'),
                            ('sponsorship_assignment', SponsorshipLmrbAssignment, 'matched_at'),
                            ('period_sponsorship_match', PeriodSponsorshipMatch, 'matched_at'),
                            ('tc_lmrb_match', TcLmrbMatch, 'matched_at')):
        qs = model.objects.filter(**{f'{f}__gt': since})
        if model is not PeriodSponsorshipMatch:
            qs = qs.filter(account_id=acc)
        else:
            qs = qs.filter(period_sponsorship__account_id=acc)
        if qs.exists():
            reasons.append(label)
    if SystemSetting.objects.filter(updated_at__gt=since).exists():
        reasons.append('settings_change')
    return Check('V5', bool(reasons), {'changed': True, 'explained_by': reasons,
                                       'previous_sha': last.sha256, 'new_sha': new_sha})
