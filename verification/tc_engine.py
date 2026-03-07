"""
TC (Transmission Certificate) Reconciliation Engine.

Workflow
--------
1. TC file is uploaded → TCRow records created in DB.
2. reconcile_tc(account_id, channel, month) is called:
   a. TC-Schedule matching:
      - For each ScheduleRow look up tc_theme via BrandMapping
      - Find unmatched TCRows with matching tc_theme + duration + channel
      - LATE-AIRED RULE: TCRow.date must be >= ScheduleRow.date
      - One-to-one (greedy, closest date first)
      - Unmatched TCRows → is_extra = True
   b. TC-LMRB cross-check:
      - For each matched (and extra) TCRow find an LMRBRow with same
        channel + date + duration AND |time_diff| <= 5 seconds
      - Sets is_lmrb_confirmed and matched_lmrb

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
    get_setting_int,
)

# ── LMRB theme helpers ────────────────────────────────────────────────────────

def _build_lmrb_theme_map(account_id):
    """
    Returns {norm_brand: [(norm_theme, duration_or_None), ...]}
    Uses BrandMapping.theme (the LMRB/MapOnline theme field).
    """
    mapping = {}
    for bm in BrandMapping.objects.filter(account_id=account_id).exclude(theme=''):
        norm_brand = _normalize(bm.brand)
        norm_theme = _normalize(bm.theme)
        dur        = int(bm.duration) if bm.duration is not None else None
        mapping.setdefault(norm_brand, []).append((norm_theme, dur))
    return mapping


def _lmrb_themes_for_brand(brand: str, duration, lmrb_theme_map: dict) -> list[str]:
    """Return list of normalised LMRB themes for a brand + optional duration."""
    nb = _normalize(brand)
    candidates = lmrb_theme_map.get(nb, [])
    dur = int(duration) if duration is not None else None
    themes = []
    for theme, map_dur in candidates:
        if map_dur is None or map_dur == dur:
            themes.append(theme)
    return themes

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
    """
    mapping = {}
    for bm in BrandMapping.objects.filter(account_id=account_id).exclude(tc_theme=''):
        norm_brand    = _normalize(bm.brand)
        norm_tc_theme = _normalize(bm.tc_theme)
        dur           = int(bm.duration) if bm.duration is not None else None
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


# ── Main reconciliation ───────────────────────────────────────────────────────

@transaction.atomic
def reconcile_tc(account_id, channel, month, mode='smart'):
    """
    Run TC-Schedule and TC-LMRB reconciliation for one scope.

    mode='smart' : skip already-matched TC rows; add new results.
    mode='reset' : clear all TC reconciliation state for scope, then re-run.

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

    # ── Reset mode ────────────────────────────────────────────────────────────
    if mode == 'reset':
        TCRow.objects.filter(account_id=account_id, channel=channel,
                             tc_report__month=month).update(
            is_schedule_matched=False, matched_schedule=None,
            is_lmrb_confirmed=False, matched_lmrb=None,
            is_extra=False,
        )

    # ── Build brand→tc_theme map ──────────────────────────────────────────────
    tc_theme_map = _build_tc_theme_map(account_id)

    # ── Load ScheduleRows for scope ───────────────────────────────────────────
    sch_qs = ScheduleRow.objects.filter(
        account_id=account_id, channel__iexact=channel, month=month,
    ).order_by('date', 'start_time')

    # ── Load unmatched TCRows for scope ───────────────────────────────────────
    # channel__iexact: handles case differences between the TC upload form and
    # the Schedule record (e.g. "Sirasa TV" vs "SIRASA TV").
    tc_qs = TCRow.objects.filter(
        account_id=account_id, channel__iexact=channel,
        tc_report__month=month,
    )
    if mode == 'smart':
        tc_qs = tc_qs.filter(is_schedule_matched=False, is_extra=False)

    # Build a mutable list of available TCRows indexed by (norm_theme, duration)
    # Each entry: (TCRow, date, aired_time_secs)
    available: dict[tuple, list] = {}
    for tcrow in tc_qs:
        key = (_normalize(tcrow.tc_theme), int(tcrow.duration) if tcrow.duration else None)
        available.setdefault(key, []).append(tcrow)
    # Sort each bucket by date ascending (prefer earliest available for greedy match)
    for key in available:
        available[key].sort(key=lambda r: r.date)

    # ── TC-Schedule matching ───────────────────────────────────────────────────
    sch_updates  = []
    tc_matched   = []

    for sr in sch_qs:
        dur = int(sr.duration) if sr.duration is not None else None
        tc_themes = _tc_themes_for_brand(sr.brand, dur, tc_theme_map)
        if not tc_themes:
            continue

        matched_row = None
        for tc_theme in tc_themes:
            key = (tc_theme, dur)
            candidates = available.get(key, [])
            # Late-aired rule: TCRow.date >= ScheduleRow.date
            valid = [r for r in candidates if r.date >= sr.date]
            if valid:
                matched_row = valid[0]  # Closest (earliest valid) date
                break

        if matched_row:
            available_key = (_normalize(matched_row.tc_theme), dur)
            available[available_key].remove(matched_row)
            matched_row.is_schedule_matched = True
            matched_row.matched_schedule    = sr
            tc_matched.append(matched_row)

    # All remaining unmatched TCRows → EXTRA
    tc_extra = []
    for rows in available.values():
        for r in rows:
            if not r.is_schedule_matched:
                r.is_extra = True
                tc_extra.append(r)

    # Bulk save TC rows
    if tc_matched or tc_extra:
        TCRow.objects.bulk_update(
            tc_matched + tc_extra,
            ['is_schedule_matched', 'matched_schedule_id', 'is_extra'],
        )

    # ── TC-LMRB cross-check ───────────────────────────────────────────────────
    # For every matched + extra TCRow, look for an LMRBRow within ±5 sec
    all_tc = list(tc_qs.filter(is_schedule_matched=True)) + list(tc_qs.filter(is_extra=True))
    # Reload freshly after bulk_update
    tc_ids = [r.id for r in (tc_matched + tc_extra)]
    if tc_ids:
        all_tc = list(TCRow.objects.filter(id__in=tc_ids))

    # Build LMRBRow index: {(norm_channel, date, duration): [(lmrb_id, time_secs, lr), ...]}
    # Use the schedule date range so only LMRB data from the same period is used.
    # This prevents LMRB rows from other months contaminating the cross-check.
    # IMPORTANT: channel key is normalised (_normalize) so case differences between
    # the TC file and the LMRB upload (e.g. "Sirasa TV" vs "SIRASA TV") never break
    # the dict lookup.
    lmrb_index: dict = {}
    lmrb_loaded = 0
    if sch_start and sch_end:
        for lr in LMRBRow.objects.filter(
            account_id=account_id, channel__iexact=channel,
            date__range=(sch_start, sch_end),
        ):
            k = (_normalize(lr.channel), lr.date, int(lr.duration) if lr.duration else None)
            lmrb_index.setdefault(k, []).append((lr.id, _time_to_secs(lr.advt_time), lr))
            lmrb_loaded += 1
    logger.debug("TC-LMRB cross-check: loaded %d LMRB rows into index (date %s–%s)",
                 lmrb_loaded, sch_start, sch_end)
    print(f"[reconcile_tc] TC-LMRB cross-check: "
          f"{lmrb_loaded} LMRB rows loaded (account={account_id}, channel='{channel}', "
          f"date={sch_start}→{sch_end})  "
          f"TC rows to check: {len(all_tc)}")

    lmrb_updates = []
    tc_lmrb_updates = []
    used_lmrb_ids = set()

    # Time tolerance is configurable by super_admin via /dashboard/settings/
    time_tolerance = get_setting_int('tc_lmrb_time_tolerance', 5)
    print(f"[reconcile_tc] TC-LMRB time tolerance: ±{time_tolerance}s")

    for tcrow in all_tc:
        tc_secs = _time_to_secs(tcrow.aired_time)
        if tc_secs is None:
            continue
        dur = int(tcrow.duration) if tcrow.duration else None
        # Normalise channel so it matches the normalised index key
        key = (_normalize(tcrow.channel), tcrow.date, dur)
        candidates = lmrb_index.get(key, [])
        best = None
        best_diff = time_tolerance + 1  # anything > tolerance means no match
        for lmrb_id, lmrb_secs, lr_obj in candidates:
            if lmrb_id in used_lmrb_ids:
                continue
            if lmrb_secs is None:
                continue
            diff = abs(tc_secs - lmrb_secs)
            if diff <= time_tolerance and diff < best_diff:
                best_diff = diff
                best = (lmrb_id, lr_obj)

        if best:
            lmrb_id, lr_obj = best
            used_lmrb_ids.add(lmrb_id)
            tcrow.is_lmrb_confirmed = True
            tcrow.matched_lmrb_id  = lmrb_id
            tc_lmrb_updates.append(tcrow)

    if tc_lmrb_updates:
        TCRow.objects.bulk_update(tc_lmrb_updates, ['is_lmrb_confirmed', 'matched_lmrb_id'])

    return {
        'matched':        len(tc_matched),
        'extra':          len(tc_extra),
        'lmrb_confirmed': len(tc_lmrb_updates),
    }


# ── Summary data builder ──────────────────────────────────────────────────────

def build_summary_data(account_id, channel, month):
    """
    Build structured summary data for the Summary Sheet report.

    Column definitions:
    - Aired       : TC rows that are schedule-matched AND LMRB-confirmed (TC ∩ LMRB)
    - 3rd Party   : Total LMRB row count for this brand/theme (independent 3rd-party count)
    - Extra       : max(0, LMRB_count - Planned)  — LMRB found more than planned
    - Missed      : max(0, Planned - LMRB_count)  — LMRB found fewer than planned

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

        exclude_spon=True: exclude rows already claimed by a SponsorshipLmrbAssignment
        (is_sponsorship_matched=True), so they are not double-counted in the
        commercial 3rd-party column.
        """
        # channel__iexact: LMRB file may store the channel name in a different case
        # (e.g. "SIRASA TV" in the Excel vs "Sirasa TV" in the schedule/TC form).
        q = LMRBRow.objects.filter(account_id=account_id, channel__iexact=channel)
        if exclude_spon:
            q = q.filter(is_sponsorship_matched=False)
        if date_min and date_max:
            q = q.filter(date__range=(date_min, date_max))
        if lmrb_themes:
            tq = Q()
            for t in lmrb_themes:
                tq |= Q(advt_theme__iexact=t)
            q = q.filter(tq)
        if dur_int is not None:
            q = q.filter(duration=dur_int)
        return q.count()

    # ── Commercial Benefits ───────────────────────────────────────────────────
    commercial_rows = []
    sch_commercial = (
        ScheduleRow.objects
        .filter(account_id=account_id, channel=channel, month=month,
                ad_type='COMMERCIAL BENEFITS')
        .values('brand', 'duration')
        .annotate(cnt=Count('id'))
        .order_by('brand', 'duration')
    )

    for group in sch_commercial:
        brand   = group['brand']
        dur     = group['duration']
        planned = group['cnt']

        tc_themes   = _tc_themes_for_brand(brand, dur, tc_theme_map)
        lmrb_themes = _lmrb_themes_for_brand(brand, dur, lmrb_theme_map)
        dur_int     = int(dur) if dur is not None else None

        # Aired = (TC schedule-matched AND LMRB-confirmed) + manually reconciled
        aired_q = TCRow.objects.filter(
            account_id=account_id, channel=channel,
            tc_report__month=month,
            is_schedule_matched=True,
            is_lmrb_confirmed=True,
        )
        if tc_themes:
            theme_q = Q()
            for t in tc_themes:
                theme_q |= Q(tc_theme__iexact=t)
            aired_q = aired_q.filter(theme_q)
        if dur_int is not None:
            aired_q = aired_q.filter(duration=dur_int)
        tc_aired = aired_q.count()

        # Count ManualMatch records for this brand/duration/month as additional aired
        manual_aired = ManualMatch.objects.filter(
            account_id=account_id,
            channel=channel,
            month=month,
            schedule_row__brand=brand,
            schedule_row__duration=dur,
            schedule_row__ad_type='COMMERCIAL BENEFITS',
        ).count()

        aired = tc_aired + manual_aired

        # 3rd Party = LMRB count excluding rows already claimed as sponsorship,
        # so sponsorship tags are not double-counted in the commercial extra column.
        third_party = _lmrb_row_count(lmrb_themes, dur_int, exclude_spon=True)

        # Extra / Missed based on LMRB vs Planned
        extra  = max(0, third_party - planned)
        missed = max(0, planned - third_party)

        avg_30 = round(aired * (dur_int or 0) / 30, 2) if dur_int else 0

        commercial_rows.append({
            'product':     brand,
            'dur':         dur_int,
            'planned':     planned,
            'aired':       aired,
            'missed':      missed,
            'extra':       extra,
            'third_party': third_party,
            'avg_30':      avg_30,
        })

    # Commercial totals
    com_total = {
        'planned':     sum(r['planned']     for r in commercial_rows),
        'aired':       sum(r['aired']       for r in commercial_rows),
        'missed':      sum(r['missed']      for r in commercial_rows),
        'extra':       sum(r['extra']       for r in commercial_rows),
        'third_party': sum(r['third_party'] for r in commercial_rows),
        'avg_30':      round(sum(r['avg_30'] for r in commercial_rows), 2),
    }

    # ── Sponsorship Benefits ──────────────────────────────────────────────────
    # Aired count = SponsorshipLmrbAssignment records (Step 1 auto + Step 2 manual).
    # This replaces the old TC-based aired count for sponsorships.
    #
    # status per row:
    #   'complete'   — aired >= planned
    #   'incomplete' — aired < planned but leftover LMRB rows exist (auto ran)
    #   'no_data'    — no brand mapping or zero leftover LMRB rows anywhere
    #
    # available_lmrb = count of unmatched LMRBRows for the scope that map to this
    # brand/duration.  Used by the "Add from LMRB" picker to decide whether to
    # show the button at all.

    def _leftover_lmrb_count(lmrb_themes, dur_int):
        """Count LMRBRows that are not matched commercially or for sponsorship."""
        q = LMRBRow.objects.filter(
            account_id=account_id, channel__iexact=channel,
            is_matched=False, is_sponsorship_matched=False,
        )
        if date_min and date_max:
            q = q.filter(date__range=(date_min, date_max))
        if lmrb_themes:
            tq = Q()
            for t in lmrb_themes:
                tq |= Q(advt_theme__iexact=t)
            q = q.filter(tq)
        if dur_int is not None:
            q = q.filter(duration=dur_int)
        return q.count()

    sponsorship_sections = []
    spon_programmes = (
        ScheduleRow.objects
        .filter(account_id=account_id, channel=channel, month=month,
                ad_type='SPONSORSHIP')
        .values_list('programme', flat=True)
        .distinct()
        .order_by('programme')
    )

    for prog in spon_programmes:
        prog_sch = (
            ScheduleRow.objects
            .filter(account_id=account_id, channel=channel, month=month,
                    ad_type='SPONSORSHIP', programme=prog)
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

            # Aired = count of SponsorshipLmrbAssignment for this brand/dur/programme
            aired = SponsorshipLmrbAssignment.objects.filter(
                account_id=account_id,
                schedule_row__channel=channel,
                schedule_row__month=month,
                schedule_row__ad_type='SPONSORSHIP',
                schedule_row__programme=prog,
                schedule_row__brand=brand,
                schedule_row__duration=dur,
            ).count()

            # Status
            if aired >= planned:
                status = 'complete'
            else:
                if lmrb_themes:
                    available_lmrb = _leftover_lmrb_count(lmrb_themes, dur_int)
                else:
                    available_lmrb = 0
                status = 'incomplete' if (aired > 0 or available_lmrb > 0) else 'no_data'

            # 3rd Party = all LMRB rows (independent monitoring count, same as commercial)
            third_party = _lmrb_row_count(lmrb_themes, dur_int)

            missed = max(0, planned - aired)
            extra  = max(0, aired - planned)
            avg_30 = round(aired * (dur_int or 0) / 30, 2) if dur_int else 0

            prog_rows.append({
                'product':     brand,
                'dur':         dur_int,
                'planned':     planned,
                'aired':       aired,
                'missed':      missed,
                'extra':       extra,
                'third_party': third_party,
                'avg_30':      avg_30,
                'status':      status,
            })

        subtotal = {
            'planned':     sum(r['planned']     for r in prog_rows),
            'aired':       sum(r['aired']       for r in prog_rows),
            'missed':      sum(r['missed']      for r in prog_rows),
            'extra':       sum(r['extra']       for r in prog_rows),
            'third_party': sum(r['third_party'] for r in prog_rows),
            'avg_30':      round(sum(r['avg_30'] for r in prog_rows), 2),
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
        'avg_30':      round(sum(s['subtotal']['avg_30'] for s in sponsorship_sections), 2),
    }

    return {
        'commercial':         commercial_rows,
        'commercial_total':   com_total,
        'sponsorship':        sponsorship_sections,
        'sponsorship_total':  spon_total,
    }
