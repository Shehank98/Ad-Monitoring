"""
Validator V1–V5 (Amendment A3). Checks follow what the code computes
(verification/tc_engine.py build_summary_data, :866-934), NOT CLAUDE.md §9.
There is deliberately no "3rd Party >= Aired" check.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.models import TransmissionReport
from verification.tc_engine import _build_tc_theme_map, _tc_themes_for_brand

from .fingerprint import diff as fingerprint_diff
from .fingerprint import fingerprint_sha
from .models import AgentAction, AgentAuthorisation, SummarySnapshot


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


def v5(scope, schedule, new_sha: str, fp_now: dict) -> Check:
    """V5 (Phase 1.1): a change in this schedule's numbers since our last snapshot is
    explained when AgentActions were logged for the scope since then, or when the
    external-change fingerprint changed (people edited mappings, uploaded, changed a
    setting, ran a core engine…). The fingerprint diff is returned for AgentRun.detail.
    A snapshot without a stored fingerprint cannot explain anything."""
    last = (SummarySnapshot.objects.filter(scope=scope, schedule=schedule)
            .exclude(kind='golden').order_by('-created_at', '-id').first())
    if last is None or last.sha256 == new_sha:
        return Check('V5', True, {'changed': False})
    reasons, fp_diff = [], {}
    if AgentAction.objects.filter(scope=scope, created_at__gt=last.created_at).exists():
        reasons.append('agent_action')
    if last.fingerprint_sha256:
        if fingerprint_sha(fp_now) != last.fingerprint_sha256:
            fp_diff = fingerprint_diff(last.fingerprint, fp_now)
            reasons.append('external_change')
    authorised = AgentAuthorisation.objects.filter(schedule=schedule).exists()
    return Check('V5', bool(reasons), {
        'changed': True, 'explained_by': reasons, 'fingerprint_diff': fp_diff,
        'authorised': authorised, 'schedule_id': schedule.id,
        'previous_sha': last.sha256, 'new_sha': new_sha, 'previous_snapshot_id': last.id})
