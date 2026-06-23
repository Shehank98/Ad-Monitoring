"""
Standalone TC ↔ LMRB Reconciliation Engine (no schedule required).

Purpose
-------
Sometimes a channel sends a Transmission Certificate (TC) for a period that has
NO booking schedule uploaded.  The normal Summary reconciliation (which is driven
by ScheduleRows) cannot run, but the user still wants to verify the TC against the
independent LMRB monitoring data — i.e. take the "LMRB cut of the TC".

This engine pairs each TCRow with an LMRBRow on:
    same channel  +  same date  +  same duration  +  |aired_time − advt_time| ≤ tol
    +  brand agreement via BrandMapping (when the tc_theme is mapped)

It is schedule-agnostic — no ScheduleRow is needed — but it DOES use BrandMapping
the same way the schedule-based engine does: the TC row's tc_theme resolves to a
brand (BrandMapping.tc_theme) and the LMRB row's advt_theme must resolve to the
same brand (BrandMapping.theme).  When a tc_theme has no mapping, matching falls
back to time-only (mirrors reconcile_tc Step 1).  The result of each pairing is
stored in a TcLmrbMatch record, and BOTH rows are locked:
    LMRBRow.is_tc_lmrb_matched = True   (global lock — every other engine skips it)
    TCRow.is_tc_lmrb_matched   = True

Public API
----------
reconcile_tc_lmrb(account_id, channel, month, mode='smart', user=None)
    Greedy one-to-one TC↔LMRB matching for one scope.
    mode='reset' first deletes existing matches for the scope (unlocking rows).
    Returns {'matched': int, 'unmatched_tc': int, 'unmatched_lmrb': int}.

build_tc_lmrb_data(account_id, channel, month)
    Returns {'matched': [...], 'unmatched_tc': [...], 'unmatched_lmrb': [...]}
    without re-running reconciliation — used by the page view and Excel export.

scope_date_range(account_id, channel, month)
    Returns (date_min, date_max) for the scope, derived from the TC reports.
"""
import logging

from django.db import transaction
from django.db.models import Max, Min, Q

from core.models import (
    LMRBRow, TCRow, TcLmrbMatch, TransmissionReport,
    get_setting_int, parse_channel_media_type,
)

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _time_to_secs(t: str):
    """Convert 'HH:MM:SS' / 'HH:MM' to seconds since midnight, else None."""
    if not t:
        return None
    parts = str(t).strip().split(':')
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(float(parts[2])) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return None


def _channel_forms(channel: str):
    """Return the set of channel spellings to match (handles 'TV - X' vs 'X')."""
    _, clean = parse_channel_media_type(channel)
    forms = {channel}
    if clean:
        forms.add(clean)
    return forms


def scope_date_range(account_id, channel, month):
    """Date window for the scope, derived from the linked TC reports.

    Falls back to the min/max TCRow dates when the report header dates are blank.
    """
    reps = TransmissionReport.objects.filter(
        account_id=account_id, channel__iexact=channel, month=month,
    )
    agg = reps.aggregate(d_min=Min('start_date'), d_max=Max('end_date'))
    d_min, d_max = agg.get('d_min'), agg.get('d_max')
    if not d_min or not d_max:
        row_agg = TCRow.objects.filter(
            account_id=account_id, channel__iexact=channel, tc_report__month=month,
        ).aggregate(d_min=Min('date'), d_max=Max('date'))
        d_min = d_min or row_agg.get('d_min')
        d_max = d_max or row_agg.get('d_max')
    return d_min, d_max


def _scope_tc_qs(account_id, channel, month):
    return TCRow.objects.filter(
        account_id=account_id, channel__iexact=channel, tc_report__month=month,
    )


def _scope_lmrb_qs(account_id, channel, month):
    """All LMRB rows that fall inside the scope's date window.

    Matches both 'TV - X' and 'X' channel spellings.
    """
    d_min, d_max = scope_date_range(account_id, channel, month)
    cq = Q()
    for f in _channel_forms(channel):
        cq |= Q(channel__iexact=f)
    qs = LMRBRow.objects.filter(cq, account_id=account_id)
    if d_min and d_max:
        qs = qs.filter(date__range=(d_min, d_max))
    return qs


# ── Main reconciliation ───────────────────────────────────────────────────────

@transaction.atomic
def reconcile_tc_lmrb(account_id, channel, month, mode='smart', user=None):
    """Greedy one-to-one TC ↔ LMRB matching for one scope (no schedule needed).

    mode='smart' : keep existing matches, only pair still-unmatched rows.
    mode='reset' : delete all matches for the scope (unlocking rows) then re-run.
    """
    if mode == 'reset':
        existing = TcLmrbMatch.objects.filter(
            account_id=account_id, channel__iexact=channel, month=month,
        )
        tc_ids   = list(existing.values_list('tc_row_id', flat=True))
        lmrb_ids = list(existing.values_list('lmrb_row_id', flat=True))
        existing.delete()
        if tc_ids:
            TCRow.objects.filter(id__in=tc_ids).update(is_tc_lmrb_matched=False)
        if lmrb_ids:
            LMRBRow.objects.filter(id__in=lmrb_ids).update(is_tc_lmrb_matched=False)

    tolerance = get_setting_int('tc_lmrb_time_tolerance', 5)

    # ── BrandMapping theme validation (same chain as the schedule-based engine) ─
    # Resolve TC tc_theme → brand (BrandMapping.tc_theme) and brand → expected LMRB
    # themes (BrandMapping.theme).  A TC row only pairs with an LMRB row whose
    # advt_theme resolves to the same brand.  When a tc_theme has NO mapping we
    # fall back to time-only matching, mirroring reconcile_tc Step 1.
    from verification.tc_engine import (
        _normalize as _norm,
        _build_reverse_tc_theme_map, _brands_for_tc_theme,
        _build_lmrb_theme_map, _lmrb_themes_for_brand,
    )
    reverse_tc_theme_map = _build_reverse_tc_theme_map(account_id)
    lmrb_theme_map       = _build_lmrb_theme_map(account_id)

    # ── Candidate TC rows: not yet matched here (and not locked elsewhere) ─────
    tc_rows = list(
        _scope_tc_qs(account_id, channel, month)
        .filter(is_tc_lmrb_matched=False)
        .order_by('date', 'aired_time')
    )

    # ── Available LMRB pool: not locked by ANY engine ─────────────────────────
    # is_matched (commercial), is_sponsorship_matched, is_manual_matched and
    # is_tc_lmrb_matched all exclude a row from being claimed here.
    lmrb_pool = list(
        _scope_lmrb_qs(account_id, channel, month).filter(
            is_matched=False,
            is_sponsorship_matched=False,
            is_manual_matched=False,
            is_tc_lmrb_matched=False,
        )
    )

    # Index pool by (date, duration) → [(secs, lr), ...]
    index: dict = {}
    for lr in lmrb_pool:
        dur = int(lr.duration) if lr.duration is not None else None
        index.setdefault((lr.date, dur), []).append((_time_to_secs(lr.advt_time), lr))

    used_lmrb_ids: set = set()
    new_matches: list = []
    matched_tc_ids: list = []
    matched_lmrb_ids: list = []

    for tc in tc_rows:
        tc_secs = _time_to_secs(tc.aired_time)
        if tc_secs is None:
            continue
        dur = int(tc.duration) if tc.duration is not None else None
        candidates = index.get((tc.date, dur), [])

        # Expected LMRB (theme, product) pairs via tc_theme → brand → lmrb_theme.
        # Empty list = tc_theme has no BrandMapping → skip theme validation.
        brands = _brands_for_tc_theme(tc.tc_theme, dur, reverse_tc_theme_map)
        expected_lmrb_pairs = []
        for brand in brands:
            expected_lmrb_pairs.extend(_lmrb_themes_for_brand(brand, dur, lmrb_theme_map))

        best = None
        best_diff = tolerance + 1
        for lmrb_secs, lr in candidates:
            if lr.id in used_lmrb_ids or lmrb_secs is None:
                continue
            # Brand validation: the LMRB row's theme/product must resolve to the
            # same brand as the TC row (when a mapping exists).  '*' suffix = prefix
            # match; '' product = match any (non-TAG brands).
            if expected_lmrb_pairs:
                lr_theme   = _norm(lr.advt_theme)
                lr_product = _norm(lr.product) if lr.product else ''
                if not any(
                    (lr_theme.startswith(t[:-1]) if t.endswith('*') else lr_theme == t)
                    and (not p or lr_product == p)
                    for t, p in expected_lmrb_pairs
                ):
                    continue
            diff = abs(tc_secs - lmrb_secs)
            if diff <= tolerance and diff < best_diff:
                best_diff = diff
                best = lr

        if best is not None:
            used_lmrb_ids.add(best.id)
            new_matches.append(TcLmrbMatch(
                account_id=account_id, channel=channel, month=month,
                tc_row=tc, lmrb_row=best, match_type='auto', matched_by=user,
            ))
            matched_tc_ids.append(tc.id)
            matched_lmrb_ids.append(best.id)

    if new_matches:
        TcLmrbMatch.objects.bulk_create(new_matches)
        TCRow.objects.filter(id__in=matched_tc_ids).update(is_tc_lmrb_matched=True)
        LMRBRow.objects.filter(id__in=matched_lmrb_ids).update(is_tc_lmrb_matched=True)

    total_tc = _scope_tc_qs(account_id, channel, month).count()
    matched  = TcLmrbMatch.objects.filter(
        account_id=account_id, channel__iexact=channel, month=month,
    ).count()
    unmatched_tc = total_tc - matched
    unmatched_lmrb = _scope_lmrb_qs(account_id, channel, month).filter(
        is_matched=False, is_sponsorship_matched=False,
        is_manual_matched=False, is_tc_lmrb_matched=False,
    ).count()

    print(
        f"[reconcile_tc_lmrb] account={account_id} channel='{channel}' month='{month}' "
        f"mode={mode} → matched={matched} (+{len(new_matches)} new), "
        f"unmatched_tc={unmatched_tc}, unmatched_lmrb={unmatched_lmrb}"
    )
    return {
        'matched':        matched,
        'unmatched_tc':   unmatched_tc,
        'unmatched_lmrb': unmatched_lmrb,
    }


# ── Manual assignment / removal ────────────────────────────────────────────────

@transaction.atomic
def assign_tc_lmrb(tc_row, lmrb_row, user=None):
    """Manually pair a TCRow with an LMRBRow (used by the picker UI).

    Returns the created TcLmrbMatch, or raises ValueError if either row is
    already taken / locked.
    """
    if tc_row.is_tc_lmrb_matched or TcLmrbMatch.objects.filter(tc_row=tc_row).exists():
        raise ValueError('This TC row is already matched.')
    if (lmrb_row.is_matched or lmrb_row.is_sponsorship_matched
            or lmrb_row.is_manual_matched or lmrb_row.is_tc_lmrb_matched
            or TcLmrbMatch.objects.filter(lmrb_row=lmrb_row).exists()):
        raise ValueError('This LMRB row is already matched or locked.')

    month = tc_row.tc_report.month
    match = TcLmrbMatch.objects.create(
        account_id=tc_row.account_id, channel=tc_row.channel, month=month,
        tc_row=tc_row, lmrb_row=lmrb_row, match_type='manual', matched_by=user,
    )
    TCRow.objects.filter(id=tc_row.id).update(is_tc_lmrb_matched=True)
    LMRBRow.objects.filter(id=lmrb_row.id).update(is_tc_lmrb_matched=True)
    return match


@transaction.atomic
def remove_tc_lmrb(match):
    """Delete a TcLmrbMatch and unlock both rows."""
    tc_id, lr_id = match.tc_row_id, match.lmrb_row_id
    match.delete()
    TCRow.objects.filter(id=tc_id).update(is_tc_lmrb_matched=False)
    LMRBRow.objects.filter(id=lr_id).update(is_tc_lmrb_matched=False)


# ── Data builder (for display + export) ────────────────────────────────────────

def build_tc_lmrb_data(account_id, channel, month):
    """Return the three lists that drive the page and the Excel download.

    matched         : list of dicts (TC spot + its confirmed LMRB spot + gap)
    unmatched_tc    : TCRows in scope with no TcLmrbMatch
    unmatched_lmrb  : LMRBRows in scope not locked by any engine
    """
    matches = (
        TcLmrbMatch.objects
        .filter(account_id=account_id, channel__iexact=channel, month=month)
        .select_related('tc_row', 'lmrb_row')
        .order_by('tc_row__date', 'tc_row__aired_time')
    )
    matched = []
    for m in matches:
        tc = m.tc_row
        lr = m.lmrb_row
        ts = _time_to_secs(tc.aired_time)
        ls = _time_to_secs(lr.advt_time)
        gap = abs(ts - ls) if (ts is not None and ls is not None) else None
        matched.append({
            'match_id':       m.id,
            'match_type':     m.match_type,
            'tc_date':        tc.date,
            'tc_time':        tc.aired_time,
            'tc_theme':       tc.tc_theme,
            'tc_programme':   tc.programme,
            'duration':       tc.duration,
            'lmrb_date':      lr.date,
            'lmrb_time':      lr.advt_time,
            'lmrb_theme':     lr.advt_theme,
            'lmrb_programme': lr.program,
            'lmrb_advertiser': lr.advertiser,
            'lmrb_product':   lr.product,
            'gap_secs':       gap,
        })

    unmatched_tc = list(
        _scope_tc_qs(account_id, channel, month)
        .filter(is_tc_lmrb_matched=False)
        .order_by('date', 'aired_time')
    )
    unmatched_lmrb = list(
        _scope_lmrb_qs(account_id, channel, month)
        .filter(
            is_matched=False, is_sponsorship_matched=False,
            is_manual_matched=False, is_tc_lmrb_matched=False,
        )
        .order_by('date', 'advt_time')
    )
    return {
        'matched':        matched,
        'unmatched_tc':   unmatched_tc,
        'unmatched_lmrb': unmatched_lmrb,
    }
