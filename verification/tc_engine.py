"""
TC (Transmission Certificate) Reconciliation Engine.

Workflow
--------
1. TC file is uploaded → TCRow records created in DB.
2. reconcile_tc(account_id, channel, month) is called:

   Step 1 — TC ↔ LMRB (ground-truth confirmation, runs FIRST):
      - For each TCRow find an LMRBRow with same channel + date + duration
        AND |aired_time − advt_time| ≤ tolerance (default 5 s)
      - Theme validation: tc_theme → BrandMapping → expected LMRB theme
      - Sets is_lmrb_confirmed + matched_lmrb; one-to-one greedy

   Step 2 — TC-confirmed LMRB → Schedule (primary path):
      - For TCRows confirmed in Step 1, find the best ScheduleRow:
          * Reverse-look up brand via tc_theme → BrandMapping
          * Prefer rows where LMRB advt_time falls in [start, end+10min]
          * Fallback: earliest ScheduleRow on or before TCRow.date
      - LATE-AIRED RULE: TCRow.date >= ScheduleRow.date
      - Sets is_schedule_matched + matched_schedule; one-to-one greedy

   Step 3 — Fallback: TC ↔ Schedule (for TCRows with no LMRB match):
      - Traditional tc_theme-based matching against remaining ScheduleRows
      - Same late-aired rule; one-to-one greedy

   Step 4 — Unmatched TCRows → is_extra = True

Public API
----------
reconcile_tc(account_id, channel, month, mode='smart')
    Runs the full reconciliation for one scope.
    mode='reset' clears existing results first.

build_summary_data(account_id, channel, month)
    Returns structured data for the Summary Sheet report without
    re-running reconciliation.
"""
import logging
from datetime import date as date_type

from django.db import transaction
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from core.models import (
    Account, BrandMapping, LMRBRow, ManualMatch, Schedule, ScheduleRow,
    SponsorshipLmrbAssignment, SummaryReportMeta, TCRow, TransmissionReport,
    get_setting_int, parse_channel_media_type,
)


def _lmrb_channel_q(channel: str) -> Q:
    """Match LMRBRow.channel handling 'TV - Sirasa TV' vs 'Sirasa TV' prefix forms."""
    _, clean = parse_channel_media_type(channel)
    if clean != channel:
        return Q(channel__iexact=channel) | Q(channel__iexact=clean)
    return Q(channel__iexact=channel)


# ── LMRB theme helpers ────────────────────────────────────────────────────────

def _build_lmrb_theme_map(account_id):
    """
    Returns {norm_brand: [(norm_theme, duration_or_None, norm_product), ...]}
    Uses BrandMapping.theme (the LMRB/MapOnline theme field).
    norm_product: '' means "match any product" (backward compat for non-TAG brands).
    """
    mapping = {}
    for bm in BrandMapping.objects.filter(account_id=account_id).exclude(theme=''):
        norm_brand   = _normalize(bm.brand)
        norm_theme   = _normalize(bm.theme)
        dur          = int(bm.duration) if bm.duration is not None else None
        norm_product = _normalize(bm.product)  # '' when not set
        mapping.setdefault(norm_brand, []).append((norm_theme, dur, norm_product))
    return mapping


def _lmrb_themes_for_brand(brand: str, duration, lmrb_theme_map: dict) -> list[tuple]:
    """Return list of (norm_theme, norm_product) pairs for a brand + optional duration.
    norm_product == '' means no product filter (match any LMRB product).
    """
    nb = _normalize(brand)
    candidates = lmrb_theme_map.get(nb, [])
    dur = int(duration) if duration is not None else None
    result = []
    for entry in candidates:
        if len(entry) == 3:
            theme, map_dur, product = entry
        else:
            theme, map_dur = entry
            product = ''
        if map_dur is None or map_dur == dur:
            result.append((theme, product))
    return result

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _time_to_secs(t: str) -> int | None:
    """Convert 'HH:MM:SS' or 'HH:MM' string to seconds since midnight."""
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


def _normalize(s: str) -> str:
    return str(s).lower().strip() if s else ''


def _build_tc_theme_map(account_id):
    """
    Returns {norm_brand: [(norm_tc_theme, duration_or_None), ...]}
    Only includes mappings that have a tc_theme set.
    tc_theme may contain multiple pipe-separated values (e.g. "Theme A|Theme B").
    Each value is added as a separate entry in the map.
    """
    mapping = {}
    for bm in BrandMapping.objects.filter(account_id=account_id).exclude(tc_theme=''):
        norm_brand = _normalize(bm.brand)
        dur        = int(bm.duration) if bm.duration is not None else None
        for raw_theme in bm.tc_theme.split('|'):
            norm_tc_theme = _normalize(raw_theme)
            if norm_tc_theme:
                mapping.setdefault(norm_brand, []).append((norm_tc_theme, dur))
    return mapping


def _tc_themes_for_brand(brand: str, duration, tc_theme_map: dict) -> list[str]:
    """Return list of normalised tc_themes for a brand + optional duration."""
    nb = _normalize(brand)
    candidates = tc_theme_map.get(nb, [])
    dur = int(duration) if duration is not None else None
    themes = []
    for tc_theme, map_dur in candidates:
        if map_dur is None or map_dur == dur:
            themes.append(tc_theme)
    return themes


def _all_tc_themes_for_brand(brand: str, tc_theme_map: dict) -> list[str]:
    """All normalised tc_themes mapped to a brand at ANY duration.
    Used for SPONSORSHIP tags, whose logged duration is unreliable across sources."""
    return [ntc for (ntc, _dur) in tc_theme_map.get(_normalize(brand), [])]


def _all_lmrb_pairs_for_brand(brand: str, lmrb_theme_map: dict) -> list[tuple]:
    """All (norm_theme, norm_product) pairs for a brand at ANY duration (sponsorship tags)."""
    out = []
    for entry in lmrb_theme_map.get(_normalize(brand), []):
        nt = entry[0]
        p  = entry[2] if len(entry) > 2 else ''
        out.append((nt, p))
    return out


def _build_reverse_tc_theme_map(account_id):
    """
    Returns {norm_tc_theme: [(norm_brand, duration_or_None), ...]}
    Reverse of _build_tc_theme_map: lets us look up brand from a TCRow's tc_theme.
    Handles pipe-separated tc_theme values.

    Wildcard suffix '*': a tc_theme like 'unlimited fiber - 20 sec*' is stored
    with the '*' preserved.  _brands_for_tc_theme handles prefix matching so any
    TCRow whose tc_theme starts with 'unlimited fiber - 20 sec' will resolve to
    the same brand (e.g. 'unlimited fiber - 20 sec extra' → same brand group).
    """
    mapping = {}
    for bm in BrandMapping.objects.filter(account_id=account_id).exclude(tc_theme=''):
        norm_brand = _normalize(bm.brand)
        dur        = int(bm.duration) if bm.duration is not None else None
        for raw_theme in bm.tc_theme.split('|'):
            norm_tc_theme = _normalize(raw_theme)
            if norm_tc_theme:
                mapping.setdefault(norm_tc_theme, []).append((norm_brand, dur))
    return mapping


def _brands_for_tc_theme(tc_theme: str, duration, reverse_tc_map: dict) -> list[str]:
    """Return list of normalised brand names for a tc_theme + optional duration.

    Supports wildcard prefix matching: if a stored key ends with '*', any TCRow
    tc_theme that starts with the prefix (before the '*') will match.
    """
    nt  = _normalize(tc_theme)
    dur = int(duration) if duration is not None else None
    brands: list[str] = []
    seen: set = set()
    for stored_theme, candidates in reverse_tc_map.items():
        # Exact match
        if stored_theme == nt:
            match = True
        # Wildcard prefix match: stored key ends with '*'
        elif stored_theme.endswith('*') and nt.startswith(stored_theme[:-1]):
            match = True
        else:
            match = False
        if match:
            for norm_brand, map_dur in candidates:
                if (map_dur is None or map_dur == dur) and norm_brand not in seen:
                    brands.append(norm_brand)
                    seen.add(norm_brand)
    return brands


# ── Main reconciliation ───────────────────────────────────────────────────────

@transaction.atomic
def reconcile_tc(account_id, channel, month, mode='smart', schedule_id=None):
    """
    Run TC-Schedule and TC-LMRB reconciliation for one scope.

    mode='smart' : skip already-matched TC rows; add new results.
    mode='reset' : clear all TC reconciliation state for scope, then re-run.
    schedule_id  : when set, restrict to ScheduleRows from that specific Schedule.

    Returns dict with summary counts.
    """
    # Get schedule date range for LMRB date filtering
    schedules = Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
    sch_dates  = schedules.aggregate(d_min=Min('start_date'), d_max=Max('end_date'))
    sch_start  = sch_dates.get('d_min')
    sch_end    = sch_dates.get('d_max')
    # Fallback: derive date range from ScheduleRow dates if Schedule header dates are missing.
    # Ensures the LMRB pool is always restricted to the same period as the schedule,
    # preventing LMRB data from other months contaminating TC-LMRB cross-checking.
    if not sch_start or not sch_end:
        row_dates = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).aggregate(d_min=Min('date'), d_max=Max('date'))
        sch_start = sch_start or row_dates.get('d_min')
        sch_end   = sch_end   or row_dates.get('d_max')

    # ── Extend date range for makeup/late-airing support ──────────────────────
    # 1. makeup_end_date on any active schedule extends sch_end so the LMRB
    #    cross-check can confirm spots that aired after the nominal schedule end.
    # 2. Makeup schedules (parent_schedule FK) linked to this scope may have rows
    #    with dates beyond sch_end; include their date range too.
    makeup_end_candidates = list(
        schedules.exclude(makeup_end_date__isnull=True)
        .values_list('makeup_end_date', flat=True)
    )
    # Find makeup schedules linked to this scope's schedules
    parent_ids = list(schedules.values_list('id', flat=True))
    makeup_schedules_qs = Schedule.objects.filter(parent_schedule_id__in=parent_ids) if parent_ids else Schedule.objects.none()
    for ms in makeup_schedules_qs:
        if ms.end_date:
            makeup_end_candidates.append(ms.end_date)
        if ms.makeup_end_date:
            makeup_end_candidates.append(ms.makeup_end_date)
    if makeup_end_candidates:
        latest_makeup = max(makeup_end_candidates)
        if sch_end is None or latest_makeup > sch_end:
            sch_end = latest_makeup

    # ── Reset mode ────────────────────────────────────────────────────────────
    if mode == 'reset':
        # Exclude TC rows pinned by a ManualMatch so manual links survive a reset.
        reset_qs = TCRow.objects.filter(
            account_id=account_id, channel=channel, tc_report__month=month,
            manual_match__isnull=True,
        )
        if schedule_id:
            # Only reset TC rows belonging to the TC report linked to this schedule,
            # so a reset for T/5617 never touches T/5588's matched TC rows.
            reset_qs = reset_qs.filter(tc_report__schedule_id=schedule_id)
        reset_qs.update(
            is_schedule_matched=False, matched_schedule=None,
            is_lmrb_confirmed=False, matched_lmrb=None,
            is_extra=False,
        )

    # ── Build theme maps ──────────────────────────────────────────────────────
    tc_theme_map         = _build_tc_theme_map(account_id)
    reverse_tc_theme_map = _build_reverse_tc_theme_map(account_id)
    lmrb_theme_map       = _build_lmrb_theme_map(account_id)

    # ── Load ScheduleRows for scope ───────────────────────────────────────────
    sch_qs = ScheduleRow.objects.filter(
        account_id=account_id, channel__iexact=channel, month=month,
    )
    if schedule_id:
        sch_qs = sch_qs.filter(schedule_id=schedule_id)
    # Include rows from makeup schedules (linked via parent_schedule) so that
    # formally rescheduled spots from a previous month are reconciled here.
    if not schedule_id and makeup_schedules_qs.exists():
        from django.db.models import Q
        makeup_sch_ids = list(makeup_schedules_qs.values_list('id', flat=True))
        sch_qs = ScheduleRow.objects.filter(
            Q(account_id=account_id, channel__iexact=channel, month=month) |
            Q(schedule_id__in=makeup_sch_ids)
        )
    sch_qs = sch_qs.order_by('date', 'start_time')
    all_sch_rows = list(sch_qs)

    # ── Load TCRows for scope ─────────────────────────────────────────────────
    # channel__iexact: handles case differences between TC upload form and Schedule.
    tc_qs = TCRow.objects.filter(
        account_id=account_id, channel__iexact=channel,
        tc_report__month=month,
    )
    if schedule_id:
        # Restrict to TC rows from the TC report explicitly linked to this schedule.
        # This prevents two schedules on the same channel+month from sharing TC rows
        # and producing inflated/incorrect matched counts.
        tc_qs = tc_qs.filter(tc_report__schedule_id=schedule_id)
    if mode == 'smart':
        # Also exclude TC rows already pinned by a ManualMatch.
        tc_qs = tc_qs.filter(
            is_schedule_matched=False, is_extra=False,
            manual_match__isnull=True,
        )
    all_tc_rows = list(tc_qs)

    # ── Build LMRB index ──────────────────────────────────────────────────────
    # {(norm_channel, date, duration): [(lmrb_id, time_secs, lr_obj), ...]}
    # Restricted to the schedule date range so data from other months is excluded.
    # Channel key is normalised so "Sirasa TV" and "SIRASA TV" both match.
    lmrb_index: dict = {}
    lmrb_loaded = 0
    if sch_start and sch_end:
        for lr in LMRBRow.objects.filter(
            _lmrb_channel_q(channel), account_id=account_id,
            date__range=(sch_start, sch_end),
            is_manual_matched=False,    # never reclaim manually locked LMRB rows
            is_tc_lmrb_matched=False,   # never reclaim standalone TC↔LMRB locked rows
        ):
            k = (_normalize(lr.channel), lr.date, int(lr.duration) if lr.duration else None)
            lmrb_index.setdefault(k, []).append((lr.id, _time_to_secs(lr.advt_time), lr))
            lmrb_loaded += 1
    logger.debug("TC-LMRB: loaded %d LMRB rows into index (date %s–%s)",
                 lmrb_loaded, sch_start, sch_end)
    print(f"[reconcile_tc] LMRB index: {lmrb_loaded} rows loaded "
          f"(account={account_id}, channel='{channel}', date={sch_start}→{sch_end}), "
          f"TC rows to process: {len(all_tc_rows)}")

    # Time tolerance is configurable via /dashboard/settings/
    time_tolerance = get_setting_int('tc_lmrb_time_tolerance', 5)
    print(f"[reconcile_tc] TC-LMRB time tolerance: ±{time_tolerance}s")

    # ── Sponsorship tags: duration-agnostic LMRB confirmation ─────────────────
    # A sponsorship "tag" is logged at different durations in each source
    # (Schedule 5s, LMRB 5s & 10s, TC 8s), so for TC rows that resolve to a
    # SPONSORSHIP brand we confirm against LMRB on channel + date + time only,
    # ignoring duration.  Commercial TC rows keep exact-duration matching.
    _spon_brands = {
        _normalize(b) for b in ScheduleRow.objects.filter(
            account_id=account_id, channel__iexact=channel, month=month,
            ad_type='SPONSORSHIP',
        ).values_list('brand', flat=True).distinct()
    }
    _spon_tc_patterns = []   # (tc_theme_pattern_norm, brand_norm)
    for _nb in _spon_brands:
        for (_ntc, _d) in tc_theme_map.get(_nb, []):
            _spon_tc_patterns.append((_ntc, _nb))
    # Date-only LMRB index (all durations), built from the duration-keyed index.
    _lmrb_by_date: dict = {}
    for (_nchan, _d, _dur), _rows in lmrb_index.items():
        _lmrb_by_date.setdefault((_nchan, _d), []).extend(_rows)

    def _spon_brands_for_tc_theme(tc_theme_norm):
        hits = []
        for pat, nb in _spon_tc_patterns:
            matched = (tc_theme_norm.startswith(pat[:-1]) if pat.endswith('*')
                       else tc_theme_norm == pat)
            if matched:
                hits.append(nb)
        return hits

    # ── STEP 1: TC ↔ LMRB (ground-truth confirmation, runs FIRST) ────────────
    # For each TCRow find the best matching LMRBRow by time proximity.
    # Theme validation: tc_theme → reverse map → brand → expected LMRB themes.
    # This ensures we don't accidentally link unrelated spots that happen to
    # share the same broadcast slot time.
    used_lmrb_ids: set = set()
    tc_lmrb_pairs: dict = {}    # tcrow.id → lr_obj
    tc_lmrb_updates: list = []  # TCRows with is_lmrb_confirmed set

    for tcrow in all_tc_rows:
        tc_secs = _time_to_secs(tcrow.aired_time)
        if tc_secs is None:
            continue
        dur = int(tcrow.duration) if tcrow.duration else None
        tc_theme_norm = _normalize(tcrow.tc_theme)
        spon_hits = _spon_brands_for_tc_theme(tc_theme_norm)

        if spon_hits:
            # Sponsorship tag → ignore duration: candidates are all LMRB rows on
            # this channel+date, and expected themes span all durations.
            candidates = _lmrb_by_date.get((_normalize(tcrow.channel), tcrow.date), [])
            expected_lmrb_pairs: list = []
            for _nb in spon_hits:
                expected_lmrb_pairs.extend(_all_lmrb_pairs_for_brand(_nb, lmrb_theme_map))
        else:
            key = (_normalize(tcrow.channel), tcrow.date, dur)
            candidates = lmrb_index.get(key, [])

            # Build expected LMRB (theme, product) pairs via tc_theme → brand → lmrb_theme chain.
            # TCRows whose tc_theme has no BrandMapping entry skip theme validation
            # (they may still confirm by time alone).
            brands = _brands_for_tc_theme(tcrow.tc_theme, dur, reverse_tc_theme_map)
            expected_lmrb_pairs = []  # list of (norm_theme, norm_product)
            for brand in brands:
                expected_lmrb_pairs.extend(_lmrb_themes_for_brand(brand, dur, lmrb_theme_map))

        best = None
        best_diff = time_tolerance + 1
        for lmrb_id, lmrb_secs, lr_obj in candidates:
            if lmrb_id in used_lmrb_ids:
                continue
            if lmrb_secs is None:
                continue
            # Skip LMRB rows whose theme+product does not match the expected set.
            # For TAG commercials: theme matches but product must also match when set.
            if expected_lmrb_pairs:
                lr_theme   = _normalize(lr_obj.advt_theme)
                lr_product = _normalize(lr_obj.product) if lr_obj.product else ''
                if not any(
                    (lr_theme.startswith(t[:-1]) if t.endswith('*') else lr_theme == t)
                    and (not p or lr_product == p)  # '' product = match any
                    for t, p in expected_lmrb_pairs
                ):
                    continue
            diff = abs(tc_secs - lmrb_secs)
            if diff <= time_tolerance and diff < best_diff:
                best_diff = diff
                best = (lmrb_id, lr_obj)

        if best:
            lmrb_id, lr_obj = best
            used_lmrb_ids.add(lmrb_id)
            tc_lmrb_pairs[tcrow.id] = lr_obj
            tcrow.is_lmrb_confirmed = True
            tcrow.matched_lmrb_id  = lmrb_id
            tc_lmrb_updates.append(tcrow)

    confirmed_tc   = [r for r in all_tc_rows if r.id in tc_lmrb_pairs]
    unconfirmed_tc = [r for r in all_tc_rows if r.id not in tc_lmrb_pairs]
    logger.debug("Step 1 complete: %d TC-LMRB confirmed, %d unconfirmed",
                 len(confirmed_tc), len(unconfirmed_tc))

    # ── STEP 2: TC-confirmed LMRB → Schedule (primary Schedule matching) ──────
    # For each TC row confirmed against LMRB, find the best ScheduleRow.
    # Using the LMRB advt_time (which is more reliable than TC aired_time for
    # window matching) we can pick the correct planned slot when multiple
    # ScheduleRows exist for the same brand/duration on the same day.
    #
    # Priority:
    #   1. ScheduleRow whose time window [start, end+10min] contains lmrb_advt_time
    #   2. Earliest ScheduleRow on or before TCRow.date (late-aired fallback)
    #
    # Both steps share the same sch_pool so a row consumed here is unavailable
    # in Step 3.

    # Build Schedule pool: {(norm_brand, duration) → [sr, ...]} sorted by date
    sch_pool: dict = {}
    for sr in all_sch_rows:
        dur = int(sr.duration) if sr.duration is not None else None
        key = (_normalize(sr.brand), dur)
        sch_pool.setdefault(key, []).append(sr)
    for key in sch_pool:
        sch_pool[key].sort(key=lambda r: r.date)

    tc_matched_via_lmrb: list = []
    # Sort by date so earlier TC-confirmed spots get priority in the pool
    for tcrow in sorted(confirmed_tc, key=lambda r: r.date):
        lr_obj = tc_lmrb_pairs[tcrow.id]
        dur    = int(tcrow.duration) if tcrow.duration is not None else None
        lmrb_secs = _time_to_secs(lr_obj.advt_time)

        brands = _brands_for_tc_theme(tcrow.tc_theme, dur, reverse_tc_theme_map)
        if not brands:
            continue  # No BrandMapping → cannot identify Schedule row

        matched_sr  = None
        matched_key = None

        for brand in brands:
            pool_key   = (brand, dur)
            candidates = sch_pool.get(pool_key, [])
            # Late-aired rule: TCRow.date >= ScheduleRow.date
            valid = [s for s in candidates if tcrow.date >= s.date]
            if not valid:
                continue

            # Pass 1: exact window match — LMRB time falls within [start, end+10min]
            if lmrb_secs is not None:
                for s in valid:
                    start_s = _time_to_secs(s.start_time)
                    end_s   = _time_to_secs(s.end_time)
                    if start_s is None or end_s is None:
                        continue
                    # Midnight-crossing window: end before start (e.g. 22:30–00:30)
                    if end_s < start_s and start_s > 43200:
                        end_s += 86400
                    # Post-midnight LMRB time (e.g. 00:53) needs the same shift
                    cmp_lmrb = lmrb_secs + 86400 if end_s > 86400 and lmrb_secs < start_s else lmrb_secs
                    if start_s <= cmp_lmrb <= end_s + 600:
                        matched_sr  = s
                        matched_key = pool_key
                        break

            # Pass 2: proximity match — closest window centre within ±2 hours
            # Only attempted when LMRB time is known but no exact window matched.
            if matched_sr is None and lmrb_secs is not None:
                best_diff = float('inf')
                for s in valid:
                    start_s = _time_to_secs(s.start_time)
                    end_s   = _time_to_secs(s.end_time)
                    if start_s is None or end_s is None:
                        continue
                    if end_s < start_s and start_s > 43200:
                        end_s += 86400
                    cmp_lmrb = lmrb_secs + 86400 if end_s > 86400 and lmrb_secs < start_s else lmrb_secs
                    centre_s = (start_s + end_s) / 2
                    diff = abs(cmp_lmrb - centre_s)
                    if diff < best_diff and diff <= 7200:
                        best_diff  = diff
                        matched_sr  = s
                        matched_key = pool_key

            # Pass 3: fallback to earliest valid date — only when no LMRB time
            # is available (e.g. unconfirmed TC row that still reaches Step 2).
            if matched_sr is None and lmrb_secs is None:
                matched_sr  = valid[0]
                matched_key = pool_key
            break

        if matched_sr and matched_key:
            sch_pool[matched_key].remove(matched_sr)
            tcrow.is_schedule_matched = True
            tcrow.matched_schedule    = matched_sr
            tc_matched_via_lmrb.append(tcrow)

    logger.debug("Step 2 complete: %d TC-confirmed rows matched to Schedule",
                 len(tc_matched_via_lmrb))

    # ── STEP 3: Fallback TC ↔ Schedule (for TCRows with no LMRB match) ────────
    # TCRows that did not find an LMRB confirmation use the traditional
    # tc_theme-based greedy matching against the remaining (unconsumed) ScheduleRows.
    available_unconfirmed: dict = {}
    for tcrow in unconfirmed_tc:
        key = (_normalize(tcrow.tc_theme), int(tcrow.duration) if tcrow.duration else None)
        available_unconfirmed.setdefault(key, []).append(tcrow)
    for key in available_unconfirmed:
        available_unconfirmed[key].sort(key=lambda r: r.date)

    tc_matched_fallback: list = []
    for sr in all_sch_rows:
        dur      = int(sr.duration) if sr.duration is not None else None
        pool_key = (_normalize(sr.brand), dur)
        # Skip rows already consumed in Step 2
        if sr not in sch_pool.get(pool_key, []):
            continue

        tc_themes = _tc_themes_for_brand(sr.brand, dur, tc_theme_map)
        if not tc_themes:
            continue

        matched_row = None
        for tc_theme in tc_themes:
            if tc_theme.endswith('*'):
                # Wildcard prefix: find all available_unconfirmed keys whose
                # tc_theme starts with the prefix (before the '*').
                prefix = tc_theme[:-1]
                matching_keys = [
                    k for k in available_unconfirmed
                    if k[1] == dur and k[0].startswith(prefix)
                ]
                for k in matching_keys:
                    valid = [r for r in available_unconfirmed[k] if r.date >= sr.date]
                    if valid:
                        matched_row = valid[0]
                        break
                if matched_row:
                    break
            else:
                key        = (tc_theme, dur)
                candidates = available_unconfirmed.get(key, [])
                # Late-aired rule: TCRow.date >= ScheduleRow.date
                valid = [r for r in candidates if r.date >= sr.date]
                if valid:
                    matched_row = valid[0]
                    break

        if matched_row:
            avail_key = (_normalize(matched_row.tc_theme), dur)
            available_unconfirmed[avail_key].remove(matched_row)
            sch_pool[pool_key].remove(sr)
            matched_row.is_schedule_matched = True
            matched_row.matched_schedule    = sr
            tc_matched_fallback.append(matched_row)

    logger.debug("Step 3 complete: %d unconfirmed TC rows matched to Schedule via tc_theme",
                 len(tc_matched_fallback))

    # ── STEP 3b: Time-belt fallback (LMRB-confirmed TC rows with no brand match) ──
    # When a confirmed TC row could not be linked to a ScheduleRow via its
    # tc_theme → BrandMapping → brand chain (e.g. the channel certificate names the
    # spot differently from the booked brand and no mapping resolves it), fall back
    # to matching purely on date + time-belt + duration using the confirmed LMRB
    # row's air time.  This catches spots that genuinely aired (LMRB-confirmed) but
    # whose tc_theme mapping is missing/incorrect, so the planned slot is no longer
    # reported as "Not Aired".  The aired programme may differ from the planned one —
    # that is surfaced separately (tc_three_way / colored schedule) rather than
    # blocking the match.
    confirmed_unmatched = [r for r in confirmed_tc if not r.is_schedule_matched]
    tc_matched_timebelt: list = []
    if confirmed_unmatched:
        # Flatten remaining (unconsumed) ScheduleRows from the shared pool,
        # remembering each row's pool key so we can remove it on match.
        # Only commercial slots are eligible — sponsorship has its own engine and
        # must not be claimed purely by a time-belt coincidence.
        remaining_pool = []  # list of (sr, pool_key)
        for pool_key, pool_rows in sch_pool.items():
            for sr in pool_rows:
                if sr.ad_type == 'COMMERCIAL BENEFITS':
                    remaining_pool.append((sr, pool_key))
        remaining_pool.sort(key=lambda pr: pr[0].date)

        used_sr_ids: set = set()
        for tcrow in sorted(confirmed_unmatched, key=lambda r: r.date):
            lr_obj     = tc_lmrb_pairs[tcrow.id]
            dur        = int(tcrow.duration) if tcrow.duration is not None else None
            aired_secs = _time_to_secs(lr_obj.advt_time)
            if aired_secs is None:
                aired_secs = _time_to_secs(tcrow.aired_time)
            if aired_secs is None:
                continue

            best_sr   = None
            best_key  = None
            best_diff = float('inf')
            for sr, pool_key in remaining_pool:
                if id(sr) in used_sr_ids:
                    continue
                sr_dur = int(sr.duration) if sr.duration is not None else None
                if sr_dur != dur:
                    continue
                # Late-aired rule: TCRow.date >= ScheduleRow.date
                if tcrow.date < sr.date:
                    continue
                start_s = _time_to_secs(sr.start_time)
                end_s   = _time_to_secs(sr.end_time)
                if start_s is None or end_s is None:
                    continue
                # Midnight-crossing window (e.g. 22:30–00:30)
                if end_s < start_s and start_s > 43200:
                    end_s += 86400
                cmp_secs = aired_secs + 86400 if end_s > 86400 and aired_secs < start_s else aired_secs
                # Exact window (+10 min grace) wins immediately
                if start_s <= cmp_secs <= end_s + 600:
                    best_sr, best_key, best_diff = sr, pool_key, -1.0
                    break
                # Otherwise keep the closest window-centre within ±2h as a soft match
                centre = (start_s + end_s) / 2
                diff   = abs(cmp_secs - centre)
                if diff < best_diff and diff <= 7200:
                    best_sr, best_key, best_diff = sr, pool_key, diff

            if best_sr is not None:
                used_sr_ids.add(id(best_sr))
                sch_pool[best_key].remove(best_sr)
                tcrow.is_schedule_matched = True
                tcrow.matched_schedule    = best_sr
                tc_matched_via_lmrb.append(tcrow)
                tc_matched_timebelt.append(tcrow)

    logger.debug("Step 3b complete: %d confirmed TC rows matched to Schedule via time-belt",
                 len(tc_matched_timebelt))

    # ── STEP 4: Remaining TCRows → EXTRA ─────────────────────────────────────
    tc_extra: list = []
    for rows in available_unconfirmed.values():
        for r in rows:
            if not r.is_schedule_matched:
                r.is_extra = True
                tc_extra.append(r)
    # Confirmed TCRows that found no Schedule match also become EXTRA
    for tcrow in confirmed_tc:
        if not tcrow.is_schedule_matched:
            tcrow.is_extra = True
            tc_extra.append(tcrow)

    tc_matched = tc_matched_via_lmrb + tc_matched_fallback

    # ── Bulk save all modified TCRows ─────────────────────────────────────────
    if tc_matched or tc_extra:
        TCRow.objects.bulk_update(
            tc_matched + tc_extra,
            ['is_schedule_matched', 'matched_schedule_id', 'is_extra'],
        )
    if tc_lmrb_updates:
        TCRow.objects.bulk_update(tc_lmrb_updates, ['is_lmrb_confirmed', 'matched_lmrb_id'])

    print(
        f"[reconcile_tc] Done — matched: {len(tc_matched)} "
        f"(via LMRB: {len(tc_matched_via_lmrb)} [incl. time-belt: {len(tc_matched_timebelt)}], "
        f"fallback: {len(tc_matched_fallback)}), "
        f"extra: {len(tc_extra)}, lmrb_confirmed: {len(tc_lmrb_updates)}"
    )

    # ── Re-apply ManualMatch records for this scope ───────────────────────────
    # Runs AFTER all engine steps so that manual links are always the final word,
    # even if a reset run cleared the TCRow flags before this reconciliation.
    # Handles two modes:
    #   tc_lmrb : TCRow confirmed by LMRB only (no schedule match intended)
    #   3way    : TCRow matched to both a ScheduleRow and an LMRBRow
    from core.models import ManualMatch as _MM
    mm_qs = _MM.objects.filter(
        account_id=account_id, channel__iexact=channel, month=month,
        tc_row__isnull=False,
    ).select_related('tc_row')

    tc_lmrb_reapply: list = []
    tc_3way_reapply: list = []
    for mm in mm_qs:
        tr = mm.tc_row
        if tr is None:
            continue
        if mm.match_mode == 'tc_lmrb':
            tr.is_lmrb_confirmed  = True
            tr.matched_lmrb_id    = mm.lmrb_row_id
            tr.is_extra           = False
            tr.is_schedule_matched = False
            tc_lmrb_reapply.append(tr)
        elif mm.match_mode == '3way':
            tr.is_schedule_matched = True
            tr.matched_schedule_id = mm.schedule_row_id
            tr.is_lmrb_confirmed   = True
            tr.matched_lmrb_id     = mm.lmrb_row_id
            tr.is_extra            = False
            tc_3way_reapply.append(tr)

    if tc_lmrb_reapply:
        TCRow.objects.bulk_update(
            tc_lmrb_reapply,
            ['is_lmrb_confirmed', 'matched_lmrb_id', 'is_extra', 'is_schedule_matched'],
        )
    if tc_3way_reapply:
        TCRow.objects.bulk_update(
            tc_3way_reapply,
            ['is_schedule_matched', 'matched_schedule_id',
             'is_lmrb_confirmed', 'matched_lmrb_id', 'is_extra'],
        )
    n_reapplied = len(tc_lmrb_reapply) + len(tc_3way_reapply)
    if n_reapplied:
        print(f"[reconcile_tc] Re-applied {n_reapplied} ManualMatch record(s) for scope.")

    return {
        'matched':        len(tc_matched),
        'extra':          len(tc_extra),
        'lmrb_confirmed': len(tc_lmrb_updates),
    }


# ── Summary data builder ──────────────────────────────────────────────────────

def build_summary_data(account_id, channel, month, schedule_id=None):
    """
    Build structured summary data for the Summary Sheet report.

    Column definitions:
    - Aired       : ALL TC rows that are LMRB-confirmed for this brand (is_lmrb_confirmed=True),
                    regardless of whether they are also schedule-matched. This includes extra TC
                    spots (no matching schedule row) that were independently confirmed by LMRB.
    - 3rd Party   : Same as Aired — both reflect the TC+LMRB confirmed count.
    - Extra       : max(0, Aired - Planned)   — more confirmed spots than planned
    - Missed      : max(0, Planned - Aired)   — fewer confirmed spots than planned

    Returns:
    {
      'commercial': [
        {
          'product': str,   # Brand name from schedule
          'dur': int,
          'planned': int,
          'aired': int,
          'missed': int,
          'extra': int,
          'third_party': int,
          'avg_30': float,
        }, ...
      ],
      'commercial_total': {...},
      'sponsorship': [
        {
          'programme': str,
          'rows': [ { same fields but without 'programme' } ]
          'subtotal': { ... }
        }, ...
      ],
      'sponsorship_total': {...},
    }
    """
    tc_theme_map   = _build_tc_theme_map(account_id)
    lmrb_theme_map = _build_lmrb_theme_map(account_id)

    # Date range for LMRB filtering (derived from the schedule)
    sch_dates = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).aggregate(d_min=Min('start_date'), d_max=Max('end_date'))
    date_min = sch_dates.get('d_min')
    date_max = sch_dates.get('d_max')

    def _lmrb_row_count(lmrb_themes, dur_int, exclude_spon=False):
        """Count LMRBRows for this brand in the scope.

        lmrb_themes: list of (norm_theme, norm_product) pairs from _lmrb_themes_for_brand.
        exclude_spon=True: exclude rows already claimed by a SponsorshipLmrbAssignment
        (is_sponsorship_matched=True), so they are not double-counted in the
        commercial 3rd-party column.
        """
        # _lmrb_channel_q handles both case differences and 'TV - Name' prefix format.
        q = LMRBRow.objects.filter(_lmrb_channel_q(channel), account_id=account_id)
        if exclude_spon:
            q = q.filter(is_sponsorship_matched=False)
        if date_min and date_max:
            q = q.filter(date__range=(date_min, date_max))
        if lmrb_themes:
            tq = Q()
            for entry in lmrb_themes:
                t = entry[0] if isinstance(entry, tuple) else entry
                tq |= Q(advt_theme__iexact=t)
            q = q.filter(tq)
        if dur_int is not None:
            q = q.filter(duration=dur_int)
        return q.count()

    # Helper: restrict ScheduleRow queries to a specific schedule when schedule_id is set
    def _sch_qs(**kwargs):
        qs = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month, **kwargs
        )
        if schedule_id:
            qs = qs.filter(schedule_id=schedule_id)
        return qs

    # ── Commercial Benefits ───────────────────────────────────────────────────
    commercial_rows = []
    sch_commercial = (
        _sch_qs(ad_type='COMMERCIAL BENEFITS')
        .values('brand', 'duration')
        .annotate(cnt=Count('id'))
        .order_by('brand', 'duration')
    )

    for group in sch_commercial:
        brand   = group['brand']
        dur     = group['duration']
        planned = group['cnt']

        tc_themes   = _tc_themes_for_brand(brand, dur, tc_theme_map)
        dur_int     = int(dur) if dur is not None else None

        # FIX 7: If there is no tc_theme mapping for this brand+duration,
        # ALL three sources (Schedule, TC, LMRB) cannot be reconciled.
        # Show Aired=0, 3rd Party=0 - do not count unverified matches.
        if not tc_themes:
            manual_q = ManualMatch.objects.filter(
                account_id=account_id, channel=channel, month=month,
                schedule_row__brand=brand, schedule_row__duration=dur,
                schedule_row__ad_type='COMMERCIAL BENEFITS',
            )
            if schedule_id:
                manual_q = manual_q.filter(schedule_row__schedule_id=schedule_id)
            manual_aired = manual_q.count()
            commercial_rows.append({
                'product':     brand,
                'dur':         dur_int,
                'planned':     planned,
                'aired':       manual_aired,   # only manual matches count without mapping
                'missed':      max(0, planned - manual_aired),
                'extra':       0,
                'third_party': 0,
            })
            continue

        # Aired = ALL TC rows that are LMRB-confirmed (schedule-matched or extra)
        aired_q = TCRow.objects.filter(
            account_id=account_id, channel=channel,
            tc_report__month=month,
            is_lmrb_confirmed=True,
        )
        if schedule_id:
            # Scope to TC rows from the report linked to this specific schedule so
            # two schedules on the same channel+month don't share TC matched counts.
            aired_q = aired_q.filter(tc_report__schedule_id=schedule_id)
        if tc_themes:
            theme_q = Q()
            for t in tc_themes:
                theme_q |= Q(tc_theme__iexact=t)
            aired_q = aired_q.filter(theme_q)
        if dur_int is not None:
            aired_q = aired_q.filter(duration=dur_int)
        tc_aired = aired_q.count()

        # Count ManualMatch records for this brand/duration/month as additional aired
        manual_q = ManualMatch.objects.filter(
            account_id=account_id, channel=channel, month=month,
            schedule_row__brand=brand, schedule_row__duration=dur,
            schedule_row__ad_type='COMMERCIAL BENEFITS',
        )
        if schedule_id:
            manual_q = manual_q.filter(schedule_row__schedule_id=schedule_id)
        manual_aired = manual_q.count()

        aired = tc_aired + manual_aired

        # 3rd Party = ALL TC rows that are LMRB-confirmed for this brand/duration.
        # This includes schedule-matched AND extra TC spots, so 3rd_party >= aired.
        # Using TC-confirmed rows (not raw LMRB) ensures both Aired and 3rd Party
        # derive from the same ground truth (TC data verified by LMRB).
        third_party_q = TCRow.objects.filter(
            account_id=account_id, channel=channel,
            tc_report__month=month,
            is_lmrb_confirmed=True,
        )
        if schedule_id:
            third_party_q = third_party_q.filter(tc_report__schedule_id=schedule_id)
        if tc_themes:
            tq = Q()
            for t in tc_themes:
                tq |= Q(tc_theme__iexact=t)
            third_party_q = third_party_q.filter(tq)
        if dur_int is not None:
            third_party_q = third_party_q.filter(duration=dur_int)
        third_party = third_party_q.count()

        # Missed = Planned − 3rd Party (TC+LMRB confirmed fewer than planned)
        missed = max(0, planned - third_party)
        # Extra = 3rd Party − Planned (TC+LMRB confirmed more than planned)
        extra  = max(0, third_party - planned)

        commercial_rows.append({
            'product':     brand,
            'dur':         dur_int,
            'planned':     planned,
            'aired':       aired,
            'missed':      missed,
            'extra':       extra,
            'third_party': third_party,
        })

    # ── No window-coverage fallback ───────────────────────────────────────────
    # A previous revision credited LMRB-confirmed TC spots whose tc_theme resolved
    # to NO brand ("unattributable" spots) to any planned commercial slot whose
    # date + time window + duration happened to cover them.
    #
    # That is unsound, and it fired precisely when it could not be trusted: the
    # pool consists *by construction* of spots with no BrandMapping, so there is
    # no evidence of which brand they belong to.  In practice it produced two
    # wrong results on the Summary Sheet:
    #   1. A brand with no tc_theme mapping showed Aired > 0, silently undoing the
    #      deliberate "no mapping → Aired = manual only, 3rd Party = 0" rule above.
    #   2. A mapped brand that did NOT air was credited with an unrelated
    #      advertiser's unmapped spot that merely fell inside its window.
    #
    # A spot now counts as Aired only when its tc_theme resolves to the brand via
    # BrandMapping, or when an operator has confirmed it with a ManualMatch.
    # Unmapped themes surface as Missed — which correctly reports the missing
    # mapping instead of hiding it behind a time-window guess.

    # Commercial totals
    com_total = {
        'planned':     sum(r['planned']     for r in commercial_rows),
        'aired':       sum(r['aired']       for r in commercial_rows),
        'missed':      sum(r['missed']      for r in commercial_rows),
        'extra':       sum(r['extra']       for r in commercial_rows),
        'third_party': sum(r['third_party'] for r in commercial_rows),
    }

    # ── Sponsorship Benefits ──────────────────────────────────────────────────
    # Aired count = SponsorshipLmrbAssignment records (Step 1 auto + Step 2 manual).
    # This replaces the old TC-based aired count for sponsorships.
    #
    # status per row:
    #   'complete'   - aired >= planned
    #   'incomplete' - aired < planned but leftover LMRB rows exist (auto ran)
    #   'no_data'    - no brand mapping or zero leftover LMRB rows anywhere
    #
    # available_lmrb = count of unmatched LMRBRows for the scope that map to this
    # brand/duration.  Used by the "Add from LMRB" picker to decide whether to
    # show the button at all.

    def _leftover_lmrb_count(lmrb_themes, dur_int):
        """Count LMRBRows that are not matched commercially or for sponsorship.

        lmrb_themes: list of (norm_theme, norm_product) pairs from _lmrb_themes_for_brand.
        """
        q = LMRBRow.objects.filter(
            _lmrb_channel_q(channel), account_id=account_id,
            is_matched=False, is_sponsorship_matched=False,
        )
        if date_min and date_max:
            q = q.filter(date__range=(date_min, date_max))
        if lmrb_themes:
            tq = Q()
            for entry in lmrb_themes:
                t = entry[0] if isinstance(entry, tuple) else entry
                tq |= Q(advt_theme__iexact=t)
            q = q.filter(tq)
        if dur_int is not None:
            q = q.filter(duration=dur_int)
        return q.count()

    sponsorship_sections = []
    spon_programmes = (
        _sch_qs(ad_type='SPONSORSHIP')
        .values_list('programme', flat=True)
        .distinct()
        .order_by('programme')
    )

    for prog in spon_programmes:
        prog_sch = (
            _sch_qs(ad_type='SPONSORSHIP', programme=prog)
            .values('brand', 'duration')
            .annotate(cnt=Count('id'))
            .order_by('brand', 'duration')
        )
        prog_rows = []
        for group in prog_sch:
            brand   = group['brand']
            dur     = group['duration']
            planned = group['cnt']
            dur_int = int(dur) if dur is not None else None

            lmrb_themes = _lmrb_themes_for_brand(brand, dur, lmrb_theme_map)

            # ── Aired (Sponsorship) ───────────────────────────────────────────
            # Combine two sources so neither is missed:
            #   a) TC-confirmed: TCRows linked to a SPONSORSHIP ScheduleRow that
            #      are also LMRB-confirmed (TCRow.is_lmrb_confirmed=True).
            #      This is the ground truth when TC reconciliation has run.
            #   b) SponsorshipLmrbAssignment: manual/auto LMRB assignments made
            #      via the sponsorship picker (used when TC data is absent).
            # Deduplication by lmrb_row_id prevents counting the same observation
            # twice if a row appears in both sources.
            tc_spon_q = TCRow.objects.filter(
                account_id=account_id, channel=channel,
                tc_report__month=month,
                is_schedule_matched=True,
                is_lmrb_confirmed=True,
                matched_schedule__ad_type='SPONSORSHIP',
                matched_schedule__programme=prog,
                matched_schedule__brand=brand,
                matched_lmrb__isnull=False,
            )
            if dur_int is not None:
                tc_spon_q = tc_spon_q.filter(matched_schedule__duration=dur_int)
            if schedule_id:
                tc_spon_q = tc_spon_q.filter(matched_schedule__schedule_id=schedule_id)

            spon_asgn_q = SponsorshipLmrbAssignment.objects.filter(
                account_id=account_id,
                schedule_row__channel=channel,
                schedule_row__month=month,
                schedule_row__ad_type='SPONSORSHIP',
                schedule_row__programme=prog,
                schedule_row__brand=brand,
                schedule_row__duration=dur,
            )
            if schedule_id:
                spon_asgn_q = spon_asgn_q.filter(schedule_row__schedule_id=schedule_id)

            # Union LMRB row IDs from both sources; len() gives unique aired count.
            tc_lmrb_ids   = set(tc_spon_q.values_list('matched_lmrb_id', flat=True))
            spon_lmrb_ids  = set(spon_asgn_q.values_list('lmrb_row_id', flat=True))

            # c) TC ↔ LMRB confirmed by tc_theme for this sponsorship brand, even
            #    when the spot was not formally linked to a sponsorship schedule
            #    row. If TC and LMRB both recorded the spot it aired, so count it
            #    (deduped by LMRB row) — genuine tag airings are no longer reported
            #    as missed just because the engine could not pin them to a row.
            tc_theme_ids: set = set()
            # Sponsorship tags: their duration differs across Schedule/LMRB/TC, so
            # resolve the tag's tc_themes at ANY duration and do NOT filter the
            # confirmed TC rows by duration — count any confirmed spot of this tag.
            tc_themes_spon = _all_tc_themes_for_brand(brand, tc_theme_map)
            if tc_themes_spon:
                _tcq = TCRow.objects.filter(
                    account_id=account_id, channel=channel,
                    tc_report__month=month,
                    is_lmrb_confirmed=True, matched_lmrb__isnull=False,
                )
                if schedule_id:
                    _tcq = _tcq.filter(tc_report__schedule_id=schedule_id)
                _tq = Q()
                for t in tc_themes_spon:
                    if t.endswith('*'):
                        _tq |= Q(tc_theme__istartswith=t[:-1])
                    else:
                        _tq |= Q(tc_theme__iexact=t)
                _tcq = _tcq.filter(_tq)
                tc_theme_ids = set(_tcq.values_list('matched_lmrb_id', flat=True))

            all_aired_ids  = tc_lmrb_ids | spon_lmrb_ids | tc_theme_ids
            aired          = len(all_aired_ids)

            # Status
            if aired >= planned:
                status = 'complete'
            else:
                if lmrb_themes:
                    available_lmrb = _leftover_lmrb_count(lmrb_themes, dur_int)
                else:
                    available_lmrb = 0
                status = 'incomplete' if (aired > 0 or available_lmrb > 0) else 'no_data'

            # 3rd Party for sponsorship = total unique LMRB observations confirmed
            # (same set as aired - TC-confirmed + SponsorshipLmrbAssignment, deduplicated).
            third_party = aired

            missed = max(0, planned - aired)
            extra  = max(0, aired - planned)

            prog_rows.append({
                'product':     brand,
                'dur':         dur_int,
                'planned':     planned,
                'aired':       aired,
                'missed':      missed,
                'extra':       extra,
                'third_party': third_party,
                'status':      status,
            })

        subtotal = {
            'planned':     sum(r['planned']     for r in prog_rows),
            'aired':       sum(r['aired']       for r in prog_rows),
            'missed':      sum(r['missed']      for r in prog_rows),
            'extra':       sum(r['extra']       for r in prog_rows),
            'third_party': sum(r['third_party'] for r in prog_rows),
        }
        sponsorship_sections.append({
            'programme': prog,
            'rows':      prog_rows,
            'subtotal':  subtotal,
        })

    spon_total = {
        'planned':     sum(s['subtotal']['planned']     for s in sponsorship_sections),
        'aired':       sum(s['subtotal']['aired']       for s in sponsorship_sections),
        'missed':      sum(s['subtotal']['missed']      for s in sponsorship_sections),
        'extra':       sum(s['subtotal']['extra']       for s in sponsorship_sections),
        'third_party': sum(s['subtotal']['third_party'] for s in sponsorship_sections),
    }

    return {
        'commercial':         commercial_rows,
        'commercial_total':   com_total,
        'sponsorship':        sponsorship_sections,
        'sponsorship_total':  spon_total,
    }
