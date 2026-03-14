"""
Sponsorship Reconciliation Engine.

Workflow
--------
After the commercial TC + LMRB + Schedule 3-way reconciliation completes, this
engine assigns leftover (unmatched) LMRBRow records to SPONSORSHIP ScheduleRows
using the same BrandMapping.theme lookup that the commercial engine uses.

Three-step Aired count strategy (per business rules):

Step 1 — Auto (reconcile_sponsorship):
  Takes LMRBRows that are NOT commercially matched (is_matched=False) and NOT
  already sponsorship-matched (is_sponsorship_matched=False), then pairs them
  one-to-one with unmatched SPONSORSHIP ScheduleRows in brand/duration order.
  When leftovers >= planned the product is fully covered; otherwise flagged
  incomplete.  Locked rows: is_sponsorship_matched=True, SponsorshipLmrbAssignment created.

Step 2 — Manual (manual_assign):
  The operations user picks specific LMRB rows from a list of remaining
  unmatched rows to cover any gap.  Same locking applies.

Step 3 — Count-only fallback:
  If a sponsorship product has no mapping and no leftover rows, build_summary_data
  in tc_engine.py will show Aired=0 with status='no_data' so the user can fill it
  manually.

Public API
----------
reconcile_sponsorship(account_id, channel, month, mode='smart')
    Run auto Step 1 matching for the scope.

lmrb_candidates(account_id, channel, month)
    Return a list of unmatched LMRBRow dicts available for manual assignment.

manual_assign(account_id, channel, month, assignments, user)
    Create manual SponsorshipLmrbAssignments.
    assignments: list of (schedule_row_id, lmrb_row_id) tuples.

reset_sponsorship(account_id, channel, month)
    Remove all SponsorshipLmrbAssignments for this scope and unlock the
    associated LMRBRows.
"""
import logging

from django.db.models import Max, Min
from django.utils import timezone

from core.models import (
    Account, BrandMapping, LMRBRow, Schedule, ScheduleRow,
    SponsorshipLmrbAssignment,
)

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(s) -> str:
    return str(s).lower().strip() if s else ''


def _build_lmrb_theme_map(account_id: int) -> dict:
    """
    Returns {norm_brand: [(norm_theme, duration_or_None), ...]}
    Uses BrandMapping.theme (the LMRB/MapOnline theme field).
    Mirrors the helper in tc_engine.py.
    """
    mapping: dict = {}
    for bm in BrandMapping.objects.filter(account_id=account_id).exclude(theme=''):
        nb = _normalize(bm.brand)
        nt = _normalize(bm.theme)
        dur = int(bm.duration) if bm.duration is not None else None
        mapping.setdefault(nb, []).append((nt, dur))
    return mapping


def _themes_for_brand(brand: str, duration, theme_map: dict) -> list:
    """Return list of normalised LMRB themes for a brand + optional duration."""
    nb  = _normalize(brand)
    dur = int(duration) if duration is not None else None
    out = []
    for nt, map_dur in theme_map.get(nb, []):
        if map_dur is None or map_dur == dur:
            out.append(nt)
    return out


def _scope_date_range(account_id, channel, month):
    """Return (date_min, date_max) for the scope; falls back to ScheduleRow dates."""
    agg = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month,
    ).aggregate(d_min=Min('start_date'), d_max=Max('end_date'))
    d_min, d_max = agg['d_min'], agg['d_max']
    if not d_min or not d_max:
        ragg = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).aggregate(d_min=Min('date'), d_max=Max('date'))
        d_min = d_min or ragg['d_min']
        d_max = d_max or ragg['d_max']
    return d_min, d_max


# ── Public API ────────────────────────────────────────────────────────────────

def reconcile_sponsorship(account_id: int, channel: str, month: str,
                          mode: str = 'smart', schedule_id=None) -> dict:
    """
    Step 1 — Auto: match leftover LMRBRows to SPONSORSHIP ScheduleRows.

    mode='smart'  Skip schedule rows that already have a SponsorshipLmrbAssignment.
    mode='reset'  Call reset_sponsorship first, then run fresh.

    Returns {'assigned': int, 'total_spon_rows': int, 'already_assigned': int}.
    """
    if mode == 'reset':
        reset_sponsorship(account_id, channel, month)

    d_min, d_max = _scope_date_range(account_id, channel, month)
    theme_map    = _build_lmrb_theme_map(account_id)

    # ── Unmatched sponsorship schedule rows ───────────────────────────────────
    spon_qs = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month,
        ad_type='SPONSORSHIP',
    )
    if schedule_id:
        spon_qs = spon_qs.filter(schedule_id=schedule_id)
    spon_qs = spon_qs.order_by('date', 'start_time', 'brand')

    total_spon = spon_qs.count()
    if total_spon == 0:
        return {'assigned': 0, 'total_spon_rows': 0, 'already_assigned': 0}

    # In smart mode, skip rows that already have an assignment
    if mode == 'smart':
        assigned_ids = set(
            SponsorshipLmrbAssignment.objects.filter(
                account_id=account_id,
                schedule_row__channel=channel,
                schedule_row__month=month,
            ).values_list('schedule_row_id', flat=True)
        )
        spon_rows = [sr for sr in spon_qs if sr.id not in assigned_ids]
        already_assigned = total_spon - len(spon_rows)
    else:
        spon_rows = list(spon_qs)
        already_assigned = 0

    if not spon_rows:
        return {'assigned': 0, 'total_spon_rows': total_spon,
                'already_assigned': already_assigned}

    # ── Leftover (unmatched) LMRBRows for this scope ──────────────────────────
    lmrb_qs = LMRBRow.objects.filter(
        account_id=account_id,
        channel__iexact=channel,
        is_matched=False,
        is_sponsorship_matched=False,
    )
    if d_min:
        lmrb_qs = lmrb_qs.filter(date__gte=d_min)
    if d_max:
        lmrb_qs = lmrb_qs.filter(date__lte=d_max)

    # Index by (norm_theme, duration) — sorted by date for stable ordering
    lmrb_pool: dict[tuple, list] = {}
    for lr in lmrb_qs.order_by('date', 'advt_time'):
        key = (_normalize(lr.advt_theme), int(lr.duration) if lr.duration is not None else None)
        lmrb_pool.setdefault(key, []).append(lr)

    # ── Greedy one-to-one matching ────────────────────────────────────────────
    used_lmrb_ids: set = set()
    new_assignments: list = []
    lmrb_to_lock:   list = []

    for sr in spon_rows:
        dur    = int(sr.duration) if sr.duration is not None else None
        themes = _themes_for_brand(sr.brand, dur, theme_map)
        if not themes:
            continue  # no brand mapping → Step 3 (count-only fallback)

        matched_lr = None
        for theme in themes:
            for lr in lmrb_pool.get((theme, dur), []):
                if lr.id not in used_lmrb_ids:
                    matched_lr = lr
                    break
            if matched_lr:
                break

        if matched_lr:
            used_lmrb_ids.add(matched_lr.id)
            new_assignments.append(SponsorshipLmrbAssignment(
                account_id   = account_id,
                schedule_row = sr,
                lmrb_row     = matched_lr,
                match_type   = 'auto',
                matched_by   = None,
            ))
            matched_lr.is_sponsorship_matched = True
            lmrb_to_lock.append(matched_lr)

    # ── Persist ───────────────────────────────────────────────────────────────
    if new_assignments:
        SponsorshipLmrbAssignment.objects.bulk_create(new_assignments)
    if lmrb_to_lock:
        LMRBRow.objects.bulk_update(lmrb_to_lock, ['is_sponsorship_matched'])

    logger.info(
        'reconcile_sponsorship: account=%s channel=%s month=%s '
        'assigned=%d total=%d already=%d',
        account_id, channel, month,
        len(new_assignments), total_spon, already_assigned,
    )
    return {
        'assigned':        len(new_assignments),
        'total_spon_rows': total_spon,
        'already_assigned': already_assigned,
    }


def lmrb_candidates(account_id: int, channel: str, month: str) -> list:
    """
    Return a list of LMRBRow dicts that are still available for manual assignment
    (not commercially matched, not sponsorship matched, within scope date range).

    Used to populate the "Add from LMRB" picker in the Summary Sheet UI.
    Each dict includes keys: id, date, advt_time, advt_theme, duration, program, source.
    """
    d_min, d_max = _scope_date_range(account_id, channel, month)

    qs = LMRBRow.objects.filter(
        account_id=account_id,
        channel__iexact=channel,
        is_matched=False,
        is_sponsorship_matched=False,
    ).order_by('advt_theme', 'duration', 'date', 'advt_time')

    if d_min:
        qs = qs.filter(date__gte=d_min)
    if d_max:
        qs = qs.filter(date__lte=d_max)

    return list(qs.values(
        'id', 'date', 'advt_time', 'advt_theme', 'duration',
        'program', 'source',
    ))


def manual_assign(account_id: int, channel: str, month: str,
                  assignments: list, user) -> dict:
    """
    Step 2 — Manual: create SponsorshipLmrbAssignments chosen by the user.

    assignments: list of (schedule_row_id, lmrb_row_id) tuples.
    Returns {'created': int, 'skipped': int}.
    """
    created = 0
    skipped = 0

    for sr_id, lr_id in assignments:
        # Guard: schedule row must be SPONSORSHIP, same scope, not already assigned
        try:
            sr = ScheduleRow.objects.get(
                id=sr_id, account_id=account_id, channel=channel,
                month=month, ad_type='SPONSORSHIP',
            )
        except ScheduleRow.DoesNotExist:
            skipped += 1
            continue

        if SponsorshipLmrbAssignment.objects.filter(schedule_row=sr).exists():
            skipped += 1
            continue

        # Guard: LMRB row must be unmatched, same account, not already locked,
        # and not permanently claimed by a ManualMatch.
        try:
            lr = LMRBRow.objects.get(
                id=lr_id, account_id=account_id,
                is_matched=False, is_sponsorship_matched=False,
                is_manual_matched=False,
            )
        except LMRBRow.DoesNotExist:
            skipped += 1
            continue

        if SponsorshipLmrbAssignment.objects.filter(lmrb_row=lr).exists():
            skipped += 1
            continue

        SponsorshipLmrbAssignment.objects.create(
            account_id   = account_id,
            schedule_row = sr,
            lmrb_row     = lr,
            match_type   = 'manual',
            matched_by   = user,
        )
        lr.is_sponsorship_matched = True
        lr.save(update_fields=['is_sponsorship_matched'])
        created += 1

    logger.info(
        'manual_assign: account=%s channel=%s month=%s created=%d skipped=%d',
        account_id, channel, month, created, skipped,
    )
    return {'created': created, 'skipped': skipped}


def reset_sponsorship(account_id: int, channel: str, month: str) -> dict:
    """
    Remove all SponsorshipLmrbAssignments for this scope and unlock the
    associated LMRBRows.  Called before a full re-run (mode='reset').
    """
    assignments = SponsorshipLmrbAssignment.objects.filter(
        account_id=account_id,
        schedule_row__channel=channel,
        schedule_row__month=month,
    )
    lmrb_ids = list(assignments.values_list('lmrb_row_id', flat=True))

    deleted, _ = assignments.delete()

    if lmrb_ids:
        LMRBRow.objects.filter(id__in=lmrb_ids).update(is_sponsorship_matched=False)

    logger.info(
        'reset_sponsorship: account=%s channel=%s month=%s deleted=%d lmrb_unlocked=%d',
        account_id, channel, month, deleted, len(lmrb_ids),
    )
    return {'deleted': deleted, 'lmrb_unlocked': len(lmrb_ids)}
