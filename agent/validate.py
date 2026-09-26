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
from .fingerprint import fingerprint_sha, relevant_diff, scope_context
from .fingerprint import is_current as fingerprint_is_current
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


def explaining_actions(scope, since):
    """AgentActions that could have changed this scope's numbers. Writes to agent or intake tables
    only (feedback, labels, V5 acknowledgements, settings) never explain a change (guardian 3.1)."""
    return (AgentAction.objects.filter(scope=scope, created_at__gt=since)
            .exclude(target_model__startswith='agent.').exclude(target_model__startswith='intake.'))


def v5(scope, schedule, new_sha: str, fp_now: dict, ctx: dict | None = None) -> Check:
    """V5: a change in this schedule's numbers since our last snapshot must be explained.

    Explained = AgentActions were logged for the scope since then, OR the part of the
    external-change fingerprint that is RELEVANT TO THIS SCOPE changed (Phase 1.2 item 3).
    The account-wide diff is returned too, for AgentRun.detail (information only).

    First-run baseline (Phase 1.2 item 2), never NEEDS_HUMAN:
      no previous snapshot                      -> baseline 'no_prior_snapshot'
      previous snapshot has no (current) fingerprint -> baseline 'baseline_no_fingerprint'
    """
    # Phase 3: only OBSERVED snapshots are a V5 baseline (never shadow / golden).
    last = (SummarySnapshot.objects.filter(scope=scope, schedule=schedule,
                                           kind__in=SummarySnapshot.BASELINE_KINDS)
            .order_by('-created_at', '-id').first())
    base = {'schedule_id': schedule.id, 'new_sha': new_sha}
    if last is None:
        return Check('V5', True, {**base, 'changed': False, 'baseline': 'no_prior_snapshot'})
    base.update(previous_sha=last.sha256, previous_snapshot_id=last.id)
    authorised = AgentAuthorisation.objects.filter(schedule=schedule).exists()
    if not fingerprint_is_current(last.fingerprint):
        if authorised and last.sha256 != new_sha:
            # Authorised numbers are never accepted on a baseline: nothing can explain it.
            return Check('V5', False, {**base, 'changed': True, 'explained_by': [],
                                       'fingerprint_diff': {}, 'relevant_diff': {},
                                       'authorised': True, 'baseline_refused': 'authorised'})
        return Check('V5', True, {**base, 'changed': last.sha256 != new_sha,
                                  'baseline': 'baseline_no_fingerprint'})
    if last.sha256 == new_sha:
        return Check('V5', True, {**base, 'changed': False})
    reasons, full, relevant = [], {}, {}
    if explaining_actions(scope, last.created_at).exists():
        reasons.append('agent_action')
    if fingerprint_sha(fp_now) != last.fingerprint_sha256:
        full = fingerprint_diff(last.fingerprint, fp_now)
        relevant = relevant_diff(full, last.fingerprint, fp_now,
                                 ctx if ctx is not None else scope_context(scope))
        if relevant:
            reasons.append('external_change')
    return Check('V5', bool(reasons), {
        **base, 'changed': True, 'explained_by': reasons, 'fingerprint_diff': full,
        'relevant_diff': relevant, 'authorised': authorised})
