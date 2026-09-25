"""
Read-only integrity queries shared by diagnose.py and agent_core_audit (A5, A10).

AUDIT EXEMPTION (numbers-guardian check 4): the LMRBRow queries in this module
deliberately INSPECT the lock flags to find inconsistent rows. They are never used to
choose candidates for matching, so they do not exclude the lock set.

Nothing here writes. Channel strings are compared as stored; the case/whitespace
folding in channel_variants() is used only to *detect* variants, never as a value.
"""
from __future__ import annotations

from collections import defaultdict

from django.db.models import Count, Q

from core.models import (
    BrandMapping, LMRBRow, ManualMatch, Schedule, ScheduleRow, TCRow, TransmissionReport,
)
from verification.engine import _lmrb_channel_q, active_schedule_ids
from verification.tc_engine import (
    _brands_for_tc_theme, _build_reverse_tc_theme_map, _build_tc_theme_map, _tc_themes_for_brand,
)

COMMERCIAL = 'COMMERCIAL BENEFITS'
LOCK_FLAGS = ('is_matched', 'is_sponsorship_matched', 'is_manual_matched', 'is_tc_lmrb_matched')


def lmrb_scope_qs(account_id, channel, date_min=None, date_max=None):
    """LMRB rows of a scope, using the engine's own channel filter (_lmrb_channel_q)."""
    qs = LMRBRow.objects.filter(_lmrb_channel_q(channel), account_id=account_id)
    if date_min and date_max:
        qs = qs.filter(date__range=(date_min, date_max))
    return qs


def multi_flag_q() -> Q:
    """LMRBRows with more than one of the four lock flags set (V2)."""
    q = Q()
    for i, a in enumerate(LOCK_FLAGS):
        for b in LOCK_FLAGS[i + 1:]:
            q |= Q(**{a: True, b: True})
    return q


def multi_flag_count(account_id, channel, date_min, date_max) -> int:
    return lmrb_scope_qs(account_id, channel, date_min, date_max).filter(multi_flag_q()).count()


# ── Mapping findings ──────────────────────────────────────────────────────────

def is_wildcard(value: str) -> bool:
    return value.strip().endswith('*')


def wildcard_tc_mappings(account_id):
    """BrandMappings with a wildcard tc_theme whose brand has COMMERCIAL rows."""
    commercial_brands = {b.lower().strip() for b in ScheduleRow.objects.filter(
        account_id=account_id, ad_type=COMMERCIAL).order_by().values_list('brand', flat=True).distinct()}
    out = []
    for bm in BrandMapping.objects.filter(account_id=account_id).exclude(tc_theme=''):
        parts = [p for p in bm.tc_theme.split('|') if p.strip()]
        if any(is_wildcard(p) for p in parts) and bm.brand.lower().strip() in commercial_brands:
            out.append(bm)
    return out


def unmapped_pairs(account_id, schedule_ids, tc_theme_map=None):
    """(brand, duration) of commercial rows with no tc_theme for that duration."""
    tc_theme_map = tc_theme_map if tc_theme_map is not None else _build_tc_theme_map(account_id)
    pairs = (ScheduleRow.objects.filter(schedule_id__in=schedule_ids, ad_type=COMMERCIAL)
             .values_list('brand', 'duration').distinct().order_by('brand', 'duration'))
    unmapped, wildcard_only = [], []
    for brand, dur in pairs:
        themes = _tc_themes_for_brand(brand, dur, tc_theme_map)
        if not themes:
            unmapped.append((brand, dur))
        elif all(t.endswith('*') for t in themes):
            wildcard_only.append((brand, dur))
    return unmapped, wildcard_only


# ── Schedule-set findings (V4) ────────────────────────────────────────────────

def duplicate_active_numbers(account_id, channel, month) -> list[str]:
    return list(Schedule.objects.filter(account_id=account_id, channel=channel, month=month,
                                        is_superseded=False)
                .values('schedule_number').annotate(n=Count('id')).filter(n__gt=1)
                .values_list('schedule_number', flat=True))


def locked_schedules(account_id, channel, month) -> list[int]:
    return list(Schedule.objects.filter(account_id=account_id, channel=channel, month=month,
                                        is_locked=True).values_list('id', flat=True))


def mixed_width_numbers(account_id, channel, month) -> list[str]:
    ids = active_schedule_ids(account_id, channel, month)
    nums = list(Schedule.objects.filter(id__in=ids).values_list('schedule_number', flat=True))
    return sorted(nums) if len({len(n) for n in nums}) > 1 else []


def superseded_count(account_id, channel, month) -> int:
    total = Schedule.objects.filter(account_id=account_id, channel=channel, month=month).count()
    return total - len(active_schedule_ids(account_id, channel, month))


# ── Lock / attribution findings ───────────────────────────────────────────────

def manual_lock_lost(account_id=None):
    """ManualMatch whose referenced ScheduleRow or LMRBRow has is_manual_matched=False.
    (TCRow has no is_manual_matched field; its pin is the reverse relation.)"""
    qs = ManualMatch.objects.filter(
        Q(schedule_row__isnull=False, schedule_row__is_manual_matched=False)
        | Q(lmrb_row__is_manual_matched=False))
    if account_id is not None:
        qs = qs.filter(account_id=account_id)
    return qs


def time_belt_unattributed(account_id, channel, month, reverse_map=None):
    """TCRows schedule-matched (not by ManualMatch) whose tc_theme resolves to no brand."""
    reverse_map = reverse_map if reverse_map is not None else _build_reverse_tc_theme_map(account_id)
    rows = (TCRow.objects.filter(account_id=account_id, channel=channel, tc_report__month=month,
                                 is_schedule_matched=True, manual_match__isnull=True)
            .values_list('id', 'tc_theme', 'duration'))
    return [rid for rid, theme, dur in rows
            if not _brands_for_tc_theme(theme, int(dur) if dur else None, reverse_map)]


def channel_variants(account_id) -> list[list[str]]:
    """Groups of stored channel strings that differ only by case or whitespace.
    The folded key is used for grouping only; the stored strings are reported as-is."""
    raw = set(Schedule.objects.filter(account_id=account_id).values_list('channel', flat=True))
    raw |= set(TransmissionReport.objects.filter(account_id=account_id).values_list('channel', flat=True))
    raw |= set(LMRBRow.objects.filter(account_id=account_id).order_by().values_list('channel', flat=True).distinct())
    groups = defaultdict(set)
    for c in raw:
        groups[' '.join(c.split()).lower()].add(c)
    return [sorted(v) for v in groups.values() if len(v) > 1]


def unlinked_tc_in_scheduled_scopes(account_id=None):
    qs = TransmissionReport.objects.filter(schedule__isnull=True)
    if account_id is not None:
        qs = qs.filter(account_id=account_id)
    return [tr for tr in qs if Schedule.objects.filter(
        account_id=tr.account_id, channel=tr.channel, month=tr.month).exists()]


# ── Makeup schedules (D25) ────────────────────────────────────────────────────

def _report_rows(schedule) -> int:
    """Commercial rows that schedule's own per-schedule report includes."""
    return ScheduleRow.objects.filter(schedule=schedule, ad_type=COMMERCIAL).count()


def makeup_links(schedule_ids=None, account_id=None) -> list[dict]:
    """One entry per parent schedule that has makeup schedules (Schedule.parent_schedule,
    related_name 'makeup_schedules'). Each schedule is reported in its own scope."""
    qs = Schedule.objects.filter(parent_schedule__isnull=False)
    if account_id is not None:
        qs = qs.filter(account_id=account_id)
    if schedule_ids is not None:
        # every parent touched by these schedules (as parent or as makeup), then the full link
        parent_ids = set(Schedule.objects.filter(id__in=schedule_ids, parent_schedule__isnull=False)
                         .values_list('parent_schedule_id', flat=True))
        parent_ids |= set(qs.filter(parent_schedule_id__in=schedule_ids)
                          .values_list('parent_schedule_id', flat=True))
        qs = qs.filter(parent_schedule_id__in=parent_ids)
    parents = {}
    for mk in qs.select_related('parent_schedule').order_by('parent_schedule_id', 'schedule_number'):
        p = mk.parent_schedule
        e = parents.setdefault(p.id, {
            'parent_id': p.id, 'parent_number': p.schedule_number, 'account_id': p.account_id,
            'parent_scope': {'channel': p.channel, 'month': p.month},
            'parent_report_rows': _report_rows(p), 'makeups': []})
        e['makeups'].append({'makeup_id': mk.id, 'makeup_number': mk.schedule_number,
                             'scope': {'channel': mk.channel, 'month': mk.month},
                             'report_rows': _report_rows(mk)})
    return list(parents.values())


def makeup_linked_ids(schedule_ids) -> set:
    """Schedules among schedule_ids that are a parent of, or a makeup for, another schedule."""
    ids = set(Schedule.objects.filter(id__in=schedule_ids, parent_schedule__isnull=False)
              .values_list('id', flat=True))
    ids |= set(Schedule.objects.filter(parent_schedule_id__in=schedule_ids)
               .values_list('parent_schedule_id', flat=True))
    return ids


def makeups_never_reconciled(account_id=None) -> list[int]:
    """Makeup schedules that are not active in their own scope (e.g. an older version),
    so no per-schedule loop would ever reconcile them."""
    qs = Schedule.objects.filter(parent_schedule__isnull=False)
    if account_id is not None:
        qs = qs.filter(account_id=account_id)
    return [s.id for s in qs if s.id not in active_schedule_ids(s.account_id, s.channel, s.month)]


# ── LOCK_ORPHANED (information only, never auto-fixed) ───────────────────────
# Relation names verified in core/models.py:
#   LMRBRow  <- TcLmrbMatch.lmrb_row         related_name='tc_lmrb_match'            (:704)
#   TCRow    <- TcLmrbMatch.tc_row           related_name='tc_lmrb_match'            (:701)
#   LMRBRow  <- SponsorshipLmrbAssignment    related_name='sponsorship_assignment'   (:577)
#   LMRBRow  <- PeriodSponsorshipMatch       related_name='period_sponsorship_match' (:664)
#   LMRBRow  <- ManualMatch.lmrb_row         related_name='manual_match'             (:527)
#   ScheduleRow <- ManualMatch.schedule_row  related_name='manual_match'             (:517)
#   LMRBRow.matched_schedule (FK field), ScheduleRow.matched_lmrb (FK field)

def lock_orphaned_querysets(account_id=None):
    """{sub_code: [(model_label, queryset), ...]}"""
    f = {} if account_id is None else {'account_id': account_id}
    return {
        'TC_LMRB': [
            ('LMRBRow', LMRBRow.objects.filter(is_tc_lmrb_matched=True, tc_lmrb_match__isnull=True, **f)),
            ('TCRow', TCRow.objects.filter(is_tc_lmrb_matched=True, tc_lmrb_match__isnull=True, **f)),
        ],
        'SPONSORSHIP': [
            ('LMRBRow', LMRBRow.objects.filter(is_sponsorship_matched=True,
                                               sponsorship_assignment__isnull=True,
                                               period_sponsorship_match__isnull=True, **f)),
        ],
        'MANUAL': [
            ('LMRBRow', LMRBRow.objects.filter(is_manual_matched=True, manual_match__isnull=True, **f)),
            ('ScheduleRow', ScheduleRow.objects.filter(is_manual_matched=True, manual_match__isnull=True, **f)),
        ],
        'COMMERCIAL': [
            ('LMRBRow', LMRBRow.objects.filter(is_matched=True, matched_schedule__isnull=True, **f)),
            ('ScheduleRow', ScheduleRow.objects.filter(is_matched=True, matched_lmrb__isnull=True, **f)),
        ],
    }
