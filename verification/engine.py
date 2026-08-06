"""
Verification engine - queries ScheduleRow and LMRBRow from the database.

Row-level locking:
  When a ScheduleRow is matched to an LMRBRow, both have is_matched=True set.
  Smart re-run queries only rows where is_matched=False, so matched pairs are
  never reprocessed or double-counted.

Reset mode:
  Unlocks all rows for the scope (is_matched=False), deletes MatchResult
  records, then runs a full fresh match.

Multi-schedule support (Rule 10):
  A single (account, channel, month) scope may have multiple Schedule records
  with different schedule_number values (e.g. different campaigns or booking
  blocks within the same month).  run_scope processes them in ascending
  schedule_number order.  The LMRB pool is shared across all schedules so a
  monitoring row consumed by schedule #101 cannot be claimed by #102.

Schedule versioning (Rule 12):
  When multiple uploads exist for the same schedule_number, only the latest
  version (highest version field) is used.

LMRB date cap (Rule 8):
  Verification is capped at the latest date for which LMRB data exists.
  Schedule rows beyond that date are left unprocessed (is_matched=False)
  and will be picked up automatically on the next LMRB upload via
  auto_run_all_for_account.

Public API
----------
run_scope(account_id, channel, month, mode='smart')
    Run matching for one (channel × month) scope.

auto_run_all_for_account(account_id)
    Run smart matching for every scope that has both Schedule and monitoring
    data.  Per-scope errors are caught and logged.
"""
import logging
from datetime import datetime

import pandas as pd
from django.db.models import Max, Min, Q
from django.utils import timezone

from core.models import Account, BrandMapping, LMRBRow, MatchResult, MonitoringData, Schedule, ScheduleRow, parse_channel_media_type
from .processing import (
    _parse_time_to_seconds, lmrb_fingerprint, match_ads, normalize,
    _get_themes_for_dur, _word_overlap_score,
    _PROG_MISMATCH_TOLERANCE, _PROG_NAME_MATCH_THRESHOLD,
)


def _lmrb_channel_q(channel: str) -> Q:
    """Return a Q filter for LMRBRow.channel that handles the 'TYPE - Name' prefix.

    Schedules store 'TV - Sirasa TV' (from the Channel model) while LMRB rows
    store the clean name 'Sirasa TV' (after _canon_channel strips the prefix).
    This filter matches both forms so reconciliation never silently drops rows.
    """
    _, clean = parse_channel_media_type(channel)
    if clean != channel:
        return Q(channel__iexact=channel) | Q(channel__iexact=clean)
    return Q(channel__iexact=channel)

logger = logging.getLogger(__name__)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_brand_theme_map(account_id):
    brand_theme_map = {}
    for bm in BrandMapping.objects.filter(account_id=account_id):
        norm_brand    = normalize(bm.brand)
        norm_theme    = normalize(bm.theme)
        mapping_dur   = int(bm.duration) if bm.duration is not None else None
        norm_product  = normalize(bm.product)  # '' when not set — skip product filter
        brand_theme_map.setdefault(norm_brand, []).append((norm_theme, mapping_dur, norm_product))
    return brand_theme_map


def _build_sch_df(sch_qs):
    """Convert a ScheduleRow queryset to a DataFrame the engine expects."""
    records = list(sch_qs.values(
        'id', 'brand', 'programme', 'date', 'start_time', 'end_time', 'duration', 'ad_type',
    ))
    if not records:
        return pd.DataFrame(columns=[
            '_sch_db_id', 'Brand', 'Programme', 'Date',
            'Start_Time', 'End_Time', 'Duration', 'Advertisement_Type',
            '_norm_brand', '_start_secs', '_end_secs', '_dur',
        ])
    df = pd.DataFrame(records)
    df.rename(columns={
        'id':         '_sch_db_id',
        'brand':      'Brand',
        'programme':  'Programme',
        'date':       'Date',
        'start_time': 'Start_Time',
        'end_time':   'End_Time',
        'duration':   'Duration',
        'ad_type':    'Advertisement_Type',
    }, inplace=True)
    df['Date']        = pd.to_datetime(df['Date'], errors='coerce')
    df['_norm_brand'] = df['Brand'].apply(normalize)
    df['_start_secs'] = df['Start_Time'].apply(_parse_time_to_seconds)
    df['_end_secs']   = df['End_Time'].apply(_parse_time_to_seconds)
    # Treat 00:00:00 end_time as midnight (86400s) when the window starts in the
    # evening (after noon).  A schedule window like 21:00–00:00 means "up to
    # midnight", not "up to start of day".
    evening_mask = (df['_end_secs'] == 0) & (df['_start_secs'] > 43200)
    df.loc[evening_mask, '_end_secs'] = 86400
    df['_dur']        = pd.to_numeric(df['Duration'], errors='coerce')
    return df.reset_index(drop=True)


def _build_mon_pool(lmrb_qs):
    """Convert a LMRBRow queryset to a monitoring pool DataFrame.

    Includes prog_time (programme start time) so the engine can apply the
    Rule 3 time-window check against Prog_time first, falling back to
    Advt_time when Prog_time is absent.
    Also includes program (programme name) for Rule 4 fuzzy tiebreaking.
    """
    records = list(lmrb_qs.values(
        'id', 'advt_theme', 'date', 'advt_time', 'duration', 'source',
        'prog_time', 'program', 'is_sponsorship_matched', 'product',
    ))
    if not records:
        return pd.DataFrame(columns=[
            '_lmrb_db_id', 'Advt_Theme', 'Date', 'Advt_time',
            'Dur', '_source', '_norm_theme', '_air_secs',
            'Prog_time', 'Program', '_prog_secs', '_is_spon_matched', '_product',
        ])
    df = pd.DataFrame(records)
    df.rename(columns={
        'id':                    '_lmrb_db_id',
        'advt_theme':            'Advt_Theme',
        'date':                  'Date',
        'advt_time':             'Advt_time',
        'duration':              'Dur',
        'source':                '_source',
        'prog_time':             'Prog_time',
        'program':               'Program',
        'is_sponsorship_matched': '_is_spon_matched',
        'product':               '_product',
    }, inplace=True)
    df['Date']        = pd.to_datetime(df['Date'], errors='coerce')
    df['_norm_theme'] = df['Advt_Theme'].apply(normalize)
    df['_air_secs']   = df['Advt_time'].apply(_parse_time_to_seconds)
    df['Dur']         = pd.to_numeric(df['Dur'], errors='coerce')
    # Rule 3: programme start time for time-window matching
    df['_prog_secs']  = df['Prog_time'].apply(_parse_time_to_seconds)
    # Normalise product for case-insensitive matching (used for TAG disambiguation)
    df['_product']    = df['_product'].apply(lambda v: str(v).lower().strip() if v else '')
    return df.reset_index(drop=True)


def _lock_matched_rows(matched_df, prog_mis_df, late_df):
    """
    Bulk-update ScheduleRow and LMRBRow with is_matched=True and FK links for
    every row that was matched, programme-mismatched, or late-telecasted.
    """
    now = timezone.now()
    sch_updates  = []
    lmrb_updates = []

    for df in [matched_df, prog_mis_df, late_df]:
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            sch_id  = row.get('_sch_row_id')
            lmrb_id = row.get('_lmrb_row_id')
            if sch_id and lmrb_id:
                sch_updates.append(ScheduleRow(
                    id=sch_id, is_matched=True,
                    matched_lmrb_id=lmrb_id, matched_at=now,
                ))
                lmrb_updates.append(LMRBRow(
                    id=lmrb_id, is_matched=True,
                    matched_schedule_id=sch_id, matched_at=now,
                ))

    if sch_updates:
        ScheduleRow.objects.bulk_update(
            sch_updates, ['is_matched', 'matched_lmrb_id', 'matched_at'],
        )
    if lmrb_updates:
        LMRBRow.objects.bulk_update(
            lmrb_updates, ['is_matched', 'matched_schedule_id', 'matched_at'],
        )


def _persist_results(account_obj, channel, month, results_by_status):
    """Bulk-create MatchResult rows from the engine output DataFrames."""
    STATUS_MAP = {
        'matched':            'matched',
        'programme mismatch': 'programme_mismatch',
        'late telecast':      'late_telecast',
        'not aired':          'not_aired',
        'no brand mapping':   'no_mapping',
        'extra aired':        'extra_aired',
    }

    def _str(v):
        return '' if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v)

    def _date(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        try:
            ts = pd.Timestamp(v)
            return ts.date() if not pd.isna(ts) else None
        except Exception:
            return None

    def _int(v):
        try:
            return int(v) if v is not None and not (isinstance(v, float) and pd.isna(v)) else None
        except Exception:
            return None

    to_save = []
    for df, default_status in results_by_status:
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            raw    = _str(r.get('Status', default_status))
            status = STATUS_MAP.get(raw.lower(), default_status)
            to_save.append(MatchResult(
                account          = account_obj,
                channel          = channel,
                month            = month,
                brand            = _str(r.get('Brand', '')),
                programme        = _str(r.get('Programme', '')),
                scheduled_date   = _date(r.get('Scheduled_Date')),
                planned_start    = _str(r.get('Planned_Start', r.get('Start_Time', ''))),
                planned_end      = _str(r.get('Planned_End', r.get('End_Time', ''))),
                duration         = _int(r.get('Duration')),
                theme            = _str(r.get('Theme', '')),
                aired_date       = _date(r.get('Aired_Date')),
                air_time         = _str(r.get('Air_Time', '')),
                source           = _str(r.get('Source', '')),
                status           = status,
                lmrb_fingerprint = _str(r.get('_lmrb_fp', '')),
                schedule_row_id  = _int(r.get('_sch_row_id')),
                lmrb_row_id      = _int(r.get('_lmrb_row_id')),
            ))
    if to_save:
        MatchResult.objects.bulk_create(to_save, batch_size=500)


def _active_schedules_for_scope(account_id, channel, month):
    """
    Return the list of Schedule objects to use for matching, in ascending
    schedule_number order.

    Rule 12 - versioning: when multiple uploads share the same schedule_number,
    only the one with the highest version number (latest upload) is kept.
    Rule 10 - ordering: the resulting distinct schedules are sorted by
    schedule_number so overlapping date ranges are processed in booking order.
    """
    all_schedules = list(
        Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
        .order_by('schedule_number', '-version')
    )
    seen_nums = set()
    active = []
    for s in all_schedules:
        if s.schedule_number not in seen_nums:
            seen_nums.add(s.schedule_number)
            active.append(s)   # first occurrence = highest version for that number
    return active   # already in schedule_number ascending order


def _makeup_schedules_for_scope(active_schedules):
    """
    Return makeup Schedule objects linked via parent_schedule FK to any of the
    active schedules.  Applies the same versioning rule (Rule 12) to deduplicate
    by schedule_number within the makeup set.
    """
    parent_ids = [s.id for s in active_schedules]
    if not parent_ids:
        return []
    all_makeup = list(
        Schedule.objects.filter(parent_schedule_id__in=parent_ids)
        .order_by('schedule_number', '-version')
    )
    seen_nums = set()
    makeup = []
    for s in all_makeup:
        if s.schedule_number not in seen_nums:
            seen_nums.add(s.schedule_number)
            makeup.append(s)
    return makeup


def active_schedule_ids(account_id, channel, month):
    """Return the list of Schedule PKs that are active for this scope (public helper)."""
    return [s.id for s in _active_schedules_for_scope(account_id, channel, month)]


def _theme_pairs_match(pairs, norm_theme):
    """True if norm_theme matches any (norm_theme_pattern, _product) pair.
    Supports the '*' wildcard-prefix used by BrandMapping themes."""
    for t, _p in pairs:
        if t.endswith('*'):
            if norm_theme.startswith(t[:-1]):
                return True
        elif norm_theme == t:
            return True
    return False


def diagnose_scope(account_id, channel, month):
    """Explain, per still-unmatched COMMERCIAL ScheduleRow, WHY it didn't match an
    LMRB (MediaWatch) spot.  Read-only — used by the Verify Ads "Why didn't this
    match?" diagnostic.  Returns a list of dicts (one per unmatched row) with a
    plain-English `reason`.
    """
    brand_theme_map = _build_brand_theme_map(account_id)

    # Full MediaWatch pool for the channel (no date cap) so we can also explain
    # spots that aired on other dates or beyond the schedule window.
    pool = list(
        LMRBRow.objects.filter(
            _lmrb_channel_q(channel), account_id=account_id, source='mediawatch',
        ).values('id', 'advt_theme', 'date', 'advt_time', 'duration',
                 'is_matched', 'matched_schedule_id', 'program')
    )
    for r in pool:
        r['_nt'] = normalize(r['advt_theme'])
        r['_secs'] = _parse_time_to_seconds(r['advt_time'])

    active_ids = active_schedule_ids(account_id, channel, month)
    rows = ScheduleRow.objects.filter(
        schedule_id__in=active_ids, ad_type='COMMERCIAL BENEFITS',
        is_matched=False, is_manual_matched=False,
    ).order_by('brand', 'date', 'start_time')

    def _fmt_delta(secs):
        secs = int(secs)
        return f'{secs // 60}m{secs % 60:02d}s'

    results = []
    for sr in rows:
        norm_brand = normalize(sr.brand)
        dur = sr.duration
        pairs = _get_themes_for_dur(brand_theme_map, norm_brand, dur)

        base = {
            'schedule_row_id': sr.id,
            'brand':    sr.brand,
            'date':     sr.date.isoformat() if sr.date else '',
            'duration': dur,
            'programme': sr.programme or '',
            'planned_start': sr.start_time or '',
            'planned_end':   sr.end_time or '',
        }

        # ── 1. Brand mapping present for this duration? ───────────────────────
        if not pairs:
            if norm_brand in brand_theme_map:
                base['reason'] = (f'Brand is mapped, but not for {dur}s. Add a mapping '
                                  f'for this brand at {dur}s, or clear the Duration on the mapping.')
            else:
                base['reason'] = 'No Brand Mapping for this brand — add one in Brand Mappings.'
            results.append(base)
            continue

        start = _parse_time_to_seconds(sr.start_time)
        end   = _parse_time_to_seconds(sr.end_time)
        if end == 0 and start and start > 43200:   # 21:00–00:00 means up to midnight
            end = 86400

        # ── 2. Any LMRB spot with this theme at all? ──────────────────────────
        theme_cands = [r for r in pool if _theme_pairs_match(pairs, r['_nt'])]
        if not theme_cands:
            base['reason'] = ("No LMRB spot found with this brand's mapped theme(s). The aired "
                              "spot may be logged under a different Advt_Theme — check the mapping "
                              "text (a '*' wildcard often catches variant suffixes).")
            results.append(base)
            continue

        # ── 3. Duration match? ────────────────────────────────────────────────
        dur_cands = [r for r in theme_cands if r['duration'] == dur] if dur is not None else theme_cands
        if not dur_cands:
            other_durs = sorted({r['duration'] for r in theme_cands if r['duration'] is not None})
            base['reason'] = (f"LMRB has this theme but at duration(s) {other_durs or 'blank'}, not "
                              f"{dur}s. Fix the LMRB Dur, or clear the Duration on the mapping.")
            results.append(base)
            continue

        # ── 4. Same-date analysis ─────────────────────────────────────────────
        same_date = [r for r in dur_cands if r['date'] == sr.date]
        if not same_date:
            other = sorted({r['date'].isoformat() for r in dur_cands if r['date']})
            base['reason'] = (f'No spot on {base["date"]}. This theme/duration aired on: '
                              f'{", ".join(other)} — those show as Late Telecast (if in-window) or Extra.')
            results.append(base)
            continue

        def _classify(r):
            """Which pass would claim this candidate? Returns (pass, delta_secs)."""
            s = r['_secs']
            if s is None:
                return (None, None)
            if start is not None and end is not None and start <= s <= end:
                return ('in-window (Matched)', 0)
            if end is not None and s > end and (s - end) <= _PROG_MISMATCH_TOLERANCE:
                return (f'{_fmt_delta(s - end)} after window (Programme Mismatch)', s - end)
            if _word_overlap_score(sr.programme, r['program']) >= _PROG_NAME_MATCH_THRESHOLD:
                return ('programme-name match (Programme Mismatch)', (s - end) if end else None)
            return (None, (s - end) if end is not None else None)

        free = [r for r in same_date if not r['is_matched']]
        # A free candidate that WOULD match under some pass → should have matched.
        matchable = [(r, _classify(r)) for r in free]
        matchable = [(r, c) for r, c in matchable if c[0] is not None]
        if matchable:
            passname = matchable[0][1][0]
            base['reason'] = (f'A matching spot aired this day ({passname}) and is still unclaimed — '
                              f'this row should match. Click Run (reset) to re-verify; if it persists, '
                              f'your live site may be on an older build than staging.')
            results.append(base)
            continue

        # No free candidate would match — say why the closest fails.
        if not free and same_date:
            base['reason'] = ('The matching spot(s) this day were already claimed by other planned '
                              'slots (more planned rows than aired spots that day).')
            results.append(base)
            continue

        # Free candidates exist but none satisfy a pass (aired far outside the window
        # and the programme name doesn't match).
        after = [r for r in free if r['_secs'] is not None and end is not None and r['_secs'] > end]
        if after:
            closest = min(after, key=lambda r: r['_secs'] - end)
            base['reason'] = (f'Aired {_fmt_delta(closest["_secs"] - end)} after the window end — beyond '
                              f'the 10-minute Programme-Mismatch limit and the programme name doesn\'t '
                              f'match, so it is Not Aired.')
        else:
            base['reason'] = 'The only same-day spot(s) aired before the planned window start.'
        results.append(base)

    return results


# ── Public API ─────────────────────────────────────────────────────────────────

def run_scope(account_id, channel, month, mode='smart'):
    """
    Load ScheduleRow + LMRBRow records from DB, run matching, persist results.

    mode='smart':  Query only is_matched=False rows.  Rows locked in previous
                   runs are skipped automatically (no fingerprint tracking needed).
    mode='reset':  Unlock all rows for this scope, delete MatchResults, then
                   run a full fresh match.

    Multiple Schedule records for the same (account, channel, month) are
    processed in ascending schedule_number order (Rule 10) using the latest
    version of each schedule_number (Rule 12).  A single LMRB pool is shared
    across all schedules so monitoring rows cannot be double-claimed.

    Verification is capped at the latest date for which LMRB data exists
    (Rule 8) - schedule rows beyond that date are left unmatched until more
    data is uploaded.

    Returns (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), total_sch.
    """
    # ── Resolve active schedules (latest version per schedule_number) ──────────
    active_schedules = _active_schedules_for_scope(account_id, channel, month)

    # ── Makeup schedules: formally rescheduled spots linked to active schedules ─
    # These are separate Schedule uploads (possibly a different month) that a
    # planner has linked via parent_schedule.  Their rows are included in this
    # scope's matching run so missed spots from the parent month are resolved.
    makeup_schedules = _makeup_schedules_for_scope(active_schedules)

    all_scope_schedules = active_schedules + makeup_schedules

    # ── Date range: union of all active schedule windows ──────────────────────
    date_start = None
    date_end   = None
    if active_schedules:
        s_dates = [s.start_date for s in active_schedules if s.start_date]
        e_dates = [s.end_date   for s in active_schedules if s.end_date]
        date_start = min(s_dates) if s_dates else None
        date_end   = max(e_dates) if e_dates else None

    # Fallback: derive range from ScheduleRow dates when Schedule headers have none
    if not date_start or not date_end:
        row_dates  = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).aggregate(min=Min('date'), max=Max('date'))
        date_start = date_start or row_dates['min']
        date_end   = date_end   or row_dates['max']

    # ── Extend date_end for makeup support ────────────────────────────────────
    # 1. makeup_end_date: planner explicitly extends the LMRB window so late-aired
    #    spots can be found after the schedule's nominal end date.
    # 2. makeup_schedules: formal reschedule uploads whose rows may have dates
    #    beyond the parent schedule's end_date.
    makeup_end_candidates = [s.makeup_end_date for s in active_schedules if s.makeup_end_date]
    for ms in makeup_schedules:
        if ms.end_date:
            makeup_end_candidates.append(ms.end_date)
        if ms.makeup_end_date:
            makeup_end_candidates.append(ms.makeup_end_date)
    if makeup_end_candidates:
        latest_makeup = max(makeup_end_candidates)
        if date_end is None or latest_makeup > date_end:
            date_end = latest_makeup

    # ── Rule 8: cap LMRB date_end at the latest available monitoring date ──────
    max_lmrb_date = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id,
    ).aggregate(d=Max('date'))['d']

    if max_lmrb_date:
        if date_end:
            date_end = min(date_end, max_lmrb_date)
        else:
            date_end = max_lmrb_date

    # Convert to pandas Timestamp for comparison inside match_ads
    max_verify_ts = pd.Timestamp(date_end) if date_end else None

    # ── Reset mode: unlock all rows for this scope ─────────────────────────────
    if mode == 'reset':
        # Unlock primary scope rows
        scope_sch_ids = list(
            ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type='COMMERCIAL BENEFITS',
            ).values_list('id', flat=True)
        )
        # Also unlock makeup schedule rows linked to this scope
        if makeup_schedules:
            makeup_sch_ids = list(
                ScheduleRow.objects.filter(
                    schedule__in=makeup_schedules, ad_type='COMMERCIAL BENEFITS',
                ).values_list('id', flat=True)
            )
            scope_sch_ids = scope_sch_ids + makeup_sch_ids

        LMRBRow.objects.filter(matched_schedule_id__in=scope_sch_ids).update(
            is_matched=False, matched_schedule=None, matched_at=None,
        )
        ScheduleRow.objects.filter(id__in=scope_sch_ids).update(
            is_matched=False, matched_lmrb=None, matched_at=None,
        )
        # Only delete non-manual MatchResult records; manual match results are
        # managed independently via ManualMatch model and must not be wiped here.
        MatchResult.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).exclude(status='manual_match').delete()

    # ── Total schedule rows count (for caller info) ────────────────────────────
    # Includes manually matched rows in the total so the planned count is accurate.
    # Also includes makeup schedule rows (linked via parent_schedule) since they
    # represent planned spots that belong to this campaign's reconciliation.
    total_sch = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month,
        ad_type='COMMERCIAL BENEFITS',
    ).count()
    if makeup_schedules:
        total_sch += ScheduleRow.objects.filter(
            schedule__in=makeup_schedules, ad_type='COMMERCIAL BENEFITS',
        ).count()

    if total_sch == 0:
        raise ValueError(
            f'No schedule rows found for "{channel}" / "{month}". '
            'Please re-upload the schedule file so rows are parsed into the database.'
        )

    # ── Smart mode: heal orphaned is_matched=True rows ────────────────────────
    # If a previous run crashed between _lock_matched_rows and _persist_results,
    # some ScheduleRows end up with is_matched=True but no MatchResult.  Smart
    # mode's is_matched=False filter then skips them permanently → they show as
    # "Pending" forever.  Detect and reset them here so this run picks them up.
    if mode == 'smart':
        scope_sch_ids_for_heal = list(
            ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type='COMMERCIAL BENEFITS', is_matched=True, is_manual_matched=False,
            ).values_list('id', flat=True)
        )
        if scope_sch_ids_for_heal:
            # Find which of those have no MatchResult
            matched_ids_with_result = set(
                MatchResult.objects.filter(
                    account_id=account_id, channel=channel, month=month,
                    schedule_row_id__in=scope_sch_ids_for_heal,
                ).exclude(status='manual_match').values_list('schedule_row_id', flat=True)
            )
            orphaned_ids = [
                sid for sid in scope_sch_ids_for_heal
                if sid not in matched_ids_with_result
            ]
            if orphaned_ids:
                # Also unlock the LMRB rows that were paired with these orphaned rows
                LMRBRow.objects.filter(matched_schedule_id__in=orphaned_ids).update(
                    is_matched=False, matched_schedule=None, matched_at=None,
                )
                ScheduleRow.objects.filter(id__in=orphaned_ids).update(
                    is_matched=False, matched_lmrb=None, matched_at=None,
                )

    # ── Build shared LMRB pool ─────────────────────────────────────────────────
    # Channel matching handles both 'TV - Sirasa TV' (schedule form) and
    # 'Sirasa TV' (LMRB after prefix stripping) via _lmrb_channel_q.
    # Only MediaWatch rows are used here; MapOnline rows are handled separately by
    # the view-only compute_maponline_scope() and never enter this authoritative pool.
    lmrb_qs = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id,
        source='mediawatch',
    )
    # Always exclude manually matched LMRB rows - permanently locked once a
    # ManualMatch record exists.  This filter applies in both smart and reset modes.
    # Also exclude rows locked by the standalone TC↔LMRB engine (tc_lmrb_engine).
    lmrb_qs = lmrb_qs.filter(is_manual_matched=False, is_tc_lmrb_matched=False)
    if mode == 'smart':
        lmrb_qs = lmrb_qs.filter(is_matched=False)
    if date_start:
        lmrb_qs = lmrb_qs.filter(date__gte=date_start)
    if date_end:
        lmrb_qs = lmrb_qs.filter(date__lte=date_end)

    mon_pool = _build_mon_pool(lmrb_qs)
    brand_theme_map = _build_brand_theme_map(account_id)

    # ── Process each schedule in order, sharing the LMRB pool ─────────────────
    # Rule 10: schedules are ordered by schedule_number ascending.
    # consumed_idx carries over - a pool row used by an earlier schedule cannot
    # be claimed again by a later one (Rule 11: "Extra Airings unless matched to
    # a second schedule" is handled naturally because later schedules see the
    # remaining unconsumed pool).
    all_matched_dfs   = []
    all_prog_mis_dfs  = []
    all_late_dfs      = []
    all_not_aired_dfs = []
    global_consumed_idx: set = set()

    # Primary schedules run first, then makeup schedules (which hold formally
    # rescheduled spots from previous months).  Both share the same LMRB pool.
    schedules_to_run = active_schedules if active_schedules else [None]
    schedules_to_run = schedules_to_run + makeup_schedules

    for sched in schedules_to_run:
        if sched is not None:
            sch_qs = ScheduleRow.objects.filter(
                schedule=sched, ad_type='COMMERCIAL BENEFITS',
            )
        else:
            # No Schedule headers found - fall back to all rows for the scope
            sch_qs = ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type='COMMERCIAL BENEFITS',
            )

        # Always exclude manually matched rows - they are locked and must never
        # appear as Not Aired, Late Telecast, or be re-processed by the engine.
        sch_qs = sch_qs.filter(is_manual_matched=False)

        if mode == 'smart':
            sch_qs = sch_qs.filter(is_matched=False)

        sch_df = _build_sch_df(sch_qs)
        if sch_df.empty:
            continue

        m_df, pm_df, l_df, na_df, _extra, consumed = match_ads(
            sch_df, mon_pool, brand_theme_map,
            pre_matched_idx=global_consumed_idx,
            max_verify_date=max_verify_ts,
        )
        global_consumed_idx = consumed

        if not m_df.empty:  all_matched_dfs.append(m_df)
        if not pm_df.empty: all_prog_mis_dfs.append(pm_df)
        if not l_df.empty:  all_late_dfs.append(l_df)
        if not na_df.empty: all_not_aired_dfs.append(na_df)

    # Early-exit for smart mode when nothing new was processed
    if (mode == 'smart'
            and not all_matched_dfs
            and not all_prog_mis_dfs
            and not all_late_dfs
            and not all_not_aired_dfs):
        empty = pd.DataFrame()
        return (empty, empty, empty, empty, empty), total_sch

    def _concat(dfs):
        filtered = [d for d in dfs if not d.empty]
        return pd.concat(filtered, ignore_index=True) if filtered else pd.DataFrame()

    matched_df   = _concat(all_matched_dfs)
    prog_mis_df  = _concat(all_prog_mis_dfs)
    late_df      = _concat(all_late_dfs)
    not_aired_df = _concat(all_not_aired_dfs)

    # ── Rule 11: Extra Airings — brand-mapped LMRB rows not consumed ────────
    # Includes ALL available LMRB data (not restricted to schedule end date)
    # so late-aired ads beyond the planned period are visible.  Only rows whose
    # theme matches a BrandMapping for this account are included.
    # Rows whose theme contains a sponsorship keyword (-BB, -Tr, etc.) are
    # excluded — they belong to sponsorship reconciliation, not commercial.
    #
    # Build theme lookup from brand_theme_map (exact + wildcard prefix).
    _extra_themes_exact = set()
    _extra_themes_prefix = []
    for _norms in brand_theme_map.values():
        for entry in _norms:
            _nt = entry[0]  # norm_theme (index 0 regardless of tuple length)
            if _nt.endswith('*'):
                _extra_themes_prefix.append(_nt[:-1])
            else:
                _extra_themes_exact.add(_nt)

    # Load sponsorship keywords to exclude from extra aired
    from core.models import get_setting_list as _get_setting_list
    _spon_keywords = [kw.lower().strip() for kw in _get_setting_list('lmrb_sponsorship_keywords') if kw.strip()]

    def _theme_is_mapped(norm_theme):
        if norm_theme in _extra_themes_exact:
            return True
        for pfx in _extra_themes_prefix:
            if norm_theme.startswith(pfx):
                return True
        return False

    def _theme_is_sponsorship(raw_theme):
        tl = raw_theme.lower().strip()
        for kw in _spon_keywords:
            if kw in tl:
                return True
        return False

    # Build wider LMRB pool: from schedule start to latest available LMRB date.
    # Restrict to mediawatch only — MapOnline rows are handled by the separate
    # MapOnline engine and must not appear here as extra aired.
    _extra_lmrb_qs = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id,
        source='mediawatch',
        is_manual_matched=False, is_sponsorship_matched=False,
    )
    if mode == 'smart':
        _extra_lmrb_qs = _extra_lmrb_qs.filter(is_matched=False)
    if date_start:
        _extra_lmrb_qs = _extra_lmrb_qs.filter(date__gte=date_start)
    # No date_end restriction — include all data up to latest LMRB date

    # Exclude IDs already in the main mon_pool to avoid double processing
    main_pool_ids = set()
    if '_lmrb_db_id' in mon_pool.columns and not mon_pool.empty:
        main_pool_ids = set(mon_pool['_lmrb_db_id'].dropna().astype(int))

    extra_rows = []
    # First: unconsumed rows from the main pool (within schedule period)
    for idx, mon_row in mon_pool.iterrows():
        if idx not in global_consumed_idx:
            if mon_row.get('_is_spon_matched', False):
                continue
            raw_theme = mon_row.get('Advt_Theme', '')
            nt = mon_row.get('_norm_theme', '')
            if not _theme_is_mapped(nt):
                continue
            if _theme_is_sponsorship(raw_theme):
                continue
            extra_rows.append({
                'Theme':      raw_theme,
                'Aired_Date': mon_row.get('Date', ''),
                'Air_Time':   mon_row.get('Advt_time', ''),
                'Duration':   mon_row.get('Dur', ''),
                'Source':     mon_row.get('_source', ''),
                'Programme':  mon_row.get('Program', ''),
                '_lmrb_row_id': mon_row.get('_lmrb_db_id'),
            })

    # Second: LMRB rows beyond schedule end date (wider pool)
    _extra_extended = _extra_lmrb_qs.exclude(id__in=main_pool_ids)
    if date_end:
        _extra_extended = _extra_extended.filter(date__gt=date_end)
    for lr in _extra_extended.values(
        'id', 'advt_theme', 'date', 'advt_time', 'duration', 'source', 'program',
    ):
        raw_theme = lr['advt_theme'] or ''
        nt = normalize(raw_theme)
        if not _theme_is_mapped(nt):
            continue
        if _theme_is_sponsorship(raw_theme):
            continue
        extra_rows.append({
            'Theme':      raw_theme,
            'Aired_Date': lr['date'],
            'Air_Time':   lr['advt_time'] or '',
            'Duration':   lr['duration'],
            'Source':     lr['source'] or '',
            'Programme':  lr['program'] or '',
            '_lmrb_row_id': lr['id'],
        })
    extra_df = pd.DataFrame(extra_rows) if extra_rows else pd.DataFrame()

    # ── Lock matched rows in DB ────────────────────────────────────────────────
    _lock_matched_rows(matched_df, prog_mis_df, late_df)

    # ── Dedup: delete stale MatchResult rows for the ScheduleRows we just
    #    processed, then re-create them fresh.  Without this, repeated smart-mode
    #    runs keep appending new not_aired / no_mapping records for the same rows
    #    (because those rows stay is_matched=False and are re-processed every time).
    processed_sch_ids = set()
    for df in (matched_df, prog_mis_df, late_df, not_aired_df):
        if df is not None and not df.empty and '_sch_row_id' in df.columns:
            processed_sch_ids.update(
                int(v) for v in df['_sch_row_id'].dropna() if v == v
            )
    if processed_sch_ids:
        MatchResult.objects.filter(
            account_id=account_id, channel=channel, month=month,
            schedule_row_id__in=processed_sch_ids,
        ).exclude(status='manual_match').delete()

    # ── Delete stale extra_aired records for this scope before re-persisting ──
    MatchResult.objects.filter(
        account_id=account_id, channel=channel, month=month,
        status='extra_aired',
    ).delete()

    # ── Persist MatchResult records ────────────────────────────────────────────
    account_obj = Account.objects.get(pk=account_id)
    _persist_results(account_obj, channel, month, [
        (matched_df,   'matched'),
        (prog_mis_df,  'programme_mismatch'),
        (late_df,      'late_telecast'),
        (not_aired_df, 'not_aired'),
        (extra_df,     'extra_aired'),
    ])

    return (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), total_sch


def auto_run_all_for_account(account_id):
    """
    Trigger smart verification for every (channel × month) scope that has both
    ScheduleRow records AND LMRBRow records for the account.

    Per-scope errors are caught - a single failure never blocks other scopes.
    Returns list of {'channel', 'month', 'ok', 'error'} dicts.
    """
    sch_channels = list(
        ScheduleRow.objects.filter(account_id=account_id)
        .values_list('channel', flat=True).distinct()
    )
    lmrb_channels_lower = {
        c.lower(): c
        for c in LMRBRow.objects.filter(account_id=account_id)
                                 .values_list('channel', flat=True).distinct()
    }
    # Case-insensitive channel overlap: use the Schedule's canonical channel name
    # so downstream queries (which filter by channel string) always work.
    overlap = [ch for ch in sch_channels if ch.lower() in lmrb_channels_lower]

    results = []
    for channel in sorted(set(overlap)):
        months = list(
            ScheduleRow.objects.filter(account_id=account_id, channel=channel)
            .values_list('month', flat=True).distinct()
        )
        for month in months:
            try:
                run_scope(account_id, channel, month, mode='smart')
                results.append({'channel': channel, 'month': month, 'ok': True})
            except Exception as exc:
                logger.warning(
                    'auto_run_all_for_account: %s/%s failed: %s', channel, month, exc,
                )
                results.append({
                    'channel': channel, 'month': month,
                    'ok': False, 'error': str(exc),
                })

    return results


# ── MapOnline preliminary matching ─────────────────────────────────────────────

def _build_maponline_brand_theme_map(account_id):
    """Build brand→theme map using BrandMapping.maponline_theme (not .theme).
    Brands with a blank maponline_theme are skipped entirely."""
    brand_theme_map = {}
    for bm in BrandMapping.objects.filter(account_id=account_id):
        if not bm.maponline_theme:
            continue
        norm_brand   = normalize(bm.brand)
        mapping_dur  = int(bm.duration) if bm.duration is not None else None
        norm_product = normalize(bm.product)  # '' = match any product
        for theme in bm.maponline_themes_list:
            brand_theme_map.setdefault(norm_brand, []).append((normalize(theme), mapping_dur, norm_product))
    return brand_theme_map


def _maponline_display_rows(df):
    """Convert a match_ads result DataFrame into a list of display dicts matching
    the shape verification.views.load_results returns (so the same Verify Ads
    front-end renderer can display MapOnline results)."""
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        sched_date = r.get('Scheduled_Date')
        aired_date = r.get('Aired_Date')
        rows.append({
            'brand':         r.get('Brand', '') or r.get('Theme', ''),
            'theme':         r.get('Theme', '') or '',
            'duration':      (int(r['Duration'])
                              if pd.notna(r.get('Duration')) and str(r.get('Duration')).strip() not in ('', 'nan')
                              else None),
            'programme':     r.get('Programme', '') or '',
            'planned_date':  str(sched_date.date()) if hasattr(sched_date, 'date')
                             else (str(sched_date) if sched_date not in (None, '') and pd.notna(sched_date) else ''),
            'planned_start': str(r.get('Planned_Start', r.get('Start_Time', '')) or ''),
            'planned_end':   str(r.get('Planned_End', r.get('End_Time', '')) or ''),
            'aired_date':    str(aired_date.date()) if hasattr(aired_date, 'date')
                             else (str(aired_date) if aired_date not in (None, '') and pd.notna(aired_date) else ''),
            'air_time':      str(r.get('Air_Time', '') or ''),
            'source':        r.get('Source', '') or 'maponline',
            'status':        r.get('Status', ''),
        })
    return rows


def compute_maponline_scope(account_id, channel, month, persist=True):
    """
    MapOnline verification: match ScheduleRows against MapOnline LMRBRows
    (source='maponline') using BrandMapping.maponline_theme, and return the
    results for display.

    Row locking (persist=True, the default):
      The scope's MapOnline locks are cleared, the whole scope is re-matched in
      one pass (so a MapOnline raw row is consumed at most once — same one-to-one
      guarantee as the MediaWatch engine), then the matched pairs are locked using
      the SEPARATE MapOnline fields (ScheduleRow.is_maponline_matched /
      LMRBRow.is_maponline_schedule_matched).  This prevents the same ad being
      matched twice while keeping MapOnline fully isolated: it never writes
      MatchResult, never touches is_matched / MediaWatch state, and never affects
      the Summary Sheet, PDFs, or the Monitoring Dashboard.

    persist=False computes and returns the same breakdown WITHOUT any DB writes
    (used by the colored-schedule export so a download has no side effects).

    Returns a dict with matched / prog_mismatch / late_telecast / not_aired /
    no_mapping / extra display-row lists plus a summary and planned/last_data info.
    """
    active_schedules = _active_schedules_for_scope(account_id, channel, month)
    makeup_schedules = _makeup_schedules_for_scope(active_schedules)

    # ── Date range: union of active schedule windows (fallback to row dates) ────
    date_start = None
    date_end   = None
    if active_schedules:
        s_dates = [s.start_date for s in active_schedules if s.start_date]
        e_dates = [s.end_date   for s in active_schedules if s.end_date]
        date_start = min(s_dates) if s_dates else None
        date_end   = max(e_dates) if e_dates else None

    if not date_start or not date_end:
        row_dates = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).aggregate(min=Min('date'), max=Max('date'))
        date_start = date_start or row_dates['min']
        date_end   = date_end   or row_dates['max']

    # Cap at latest MapOnline date available for this scope
    max_maponline_date = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id, source='maponline',
    ).aggregate(d=Max('date'))['d']
    if max_maponline_date:
        date_end = min(date_end, max_maponline_date) if date_end else max_maponline_date
    max_verify_ts = pd.Timestamp(date_end) if date_end else None

    planned = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month,
        ad_type='COMMERCIAL BENEFITS',
    ).count()
    if makeup_schedules:
        planned += ScheduleRow.objects.filter(
            schedule__in=makeup_schedules, ad_type='COMMERCIAL BENEFITS',
        ).count()

    empty = {
        'matched': [], 'prog_mismatch': [], 'late_telecast': [],
        'not_aired': [], 'no_mapping': [], 'extra': [],
        'planned': planned, 'max_data_date': str(max_maponline_date) if max_maponline_date else None,
    }

    # ── Reset this scope's MapOnline locks so each run re-matches from scratch ──
    # (keeps the returned breakdown complete and prevents stale one-to-one locks).
    if persist:
        scope_sch_ids = list(
            ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type='COMMERCIAL BENEFITS',
            ).values_list('id', flat=True)
        )
        if makeup_schedules:
            scope_sch_ids += list(
                ScheduleRow.objects.filter(
                    schedule__in=makeup_schedules, ad_type='COMMERCIAL BENEFITS',
                ).values_list('id', flat=True)
            )
        LMRBRow.objects.filter(
            maponline_schedule_matches__id__in=scope_sch_ids,
        ).update(is_maponline_schedule_matched=False)
        ScheduleRow.objects.filter(id__in=scope_sch_ids).update(
            is_maponline_matched=False,
            matched_maponline_lmrb=None,
            maponline_matched_at=None,
        )

    # Build MapOnline LMRB pool (source='maponline' only; skip manually-locked rows)
    lmrb_qs = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id,
        source='maponline',
        is_manual_matched=False,
    )
    if date_start:
        lmrb_qs = lmrb_qs.filter(date__gte=date_start)
    if date_end:
        lmrb_qs = lmrb_qs.filter(date__lte=date_end)

    mon_pool = _build_mon_pool(lmrb_qs)
    brand_theme_map = _build_maponline_brand_theme_map(account_id)

    if mon_pool.empty and not brand_theme_map:
        return empty

    matched, prog_mis, late, not_aired, no_mapping, extra = [], [], [], [], [], []
    lock_pairs = []   # (schedule_row_id, lmrb_row_id) to lock when persist=True
    global_consumed_idx: set = set()

    schedules_to_run = (active_schedules if active_schedules else [None]) + makeup_schedules

    for sched in schedules_to_run:
        if sched is not None:
            sch_qs = ScheduleRow.objects.filter(schedule=sched, ad_type='COMMERCIAL BENEFITS')
        else:
            sch_qs = ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type='COMMERCIAL BENEFITS',
            )
        # Skip rows the operator manually reconciled (locked out of auto engines).
        sch_qs = sch_qs.filter(is_manual_matched=False)

        sch_df = _build_sch_df(sch_qs)
        if sch_df.empty:
            continue

        m_df, pm_df, late_df, na_df, _extra_df, consumed = match_ads(
            sch_df, mon_pool, brand_theme_map,
            pre_matched_idx=global_consumed_idx,
            max_verify_date=max_verify_ts,
        )
        global_consumed_idx = consumed

        matched.extend(_maponline_display_rows(m_df))
        prog_mis.extend(_maponline_display_rows(pm_df))
        late.extend(_maponline_display_rows(late_df))
        # na_df mixes 'Not Aired' and 'No Brand Mapping' statuses — split them.
        for disp in _maponline_display_rows(na_df):
            (no_mapping if disp['status'] == 'No Brand Mapping' else not_aired).append(disp)

        # Collect (schedule, lmrb) pairs consumed this pass so we can lock them.
        # matched / programme_mismatch / late_telecast each consume one LMRB row.
        for _df in (m_df, pm_df, late_df):
            if _df is None or _df.empty:
                continue
            for _, _r in _df.iterrows():
                _sid = _r.get('_sch_row_id')
                _lid = _r.get('_lmrb_row_id')
                if _sid and _lid:
                    lock_pairs.append((_sid, _lid))

    # ── Persist MapOnline locks (separate fields — never affects MediaWatch) ────
    if persist and lock_pairs:
        now = timezone.now()
        sch_updates = [
            ScheduleRow(
                id=sid, is_maponline_matched=True,
                matched_maponline_lmrb_id=lid, maponline_matched_at=now,
            )
            for sid, lid in lock_pairs
        ]
        lmrb_updates = [
            LMRBRow(id=lid, is_maponline_schedule_matched=True)
            for _sid, lid in lock_pairs
        ]
        ScheduleRow.objects.bulk_update(
            sch_updates,
            ['is_maponline_matched', 'matched_maponline_lmrb_id', 'maponline_matched_at'],
        )
        LMRBRow.objects.bulk_update(lmrb_updates, ['is_maponline_schedule_matched'])

    # ── Extra Aired: MapOnline rows left unconsumed across all schedules ────────
    # Built from the leftover pool (not match_ads's per-pass extra_df) so it can
    # carry the aired programme (LMRBRow.program, sourced from the file's Prg Name
    # column) — same as the MediaWatch engine's extra rows.
    for idx, mon_row in mon_pool.iterrows():
        if idx in global_consumed_idx:
            continue
        aired = mon_row.get('Date', '')
        extra.append({
            'brand':      mon_row.get('Advt_Theme', '') or '',
            'theme':      mon_row.get('Advt_Theme', '') or '',
            'duration':   (int(mon_row['Dur'])
                           if pd.notna(mon_row.get('Dur')) and str(mon_row.get('Dur')).strip() not in ('', 'nan')
                           else None),
            'programme':  mon_row.get('Program', '') or '',
            'aired_date': str(aired.date()) if hasattr(aired, 'date')
                          else (str(aired) if aired not in (None, '') and pd.notna(aired) else ''),
            'air_time':   str(mon_row.get('Advt_time', '') or ''),
            'source':     mon_row.get('_source', '') or 'maponline',
            'status':     'Extra Aired',
        })

    return {
        'matched':       matched,
        'prog_mismatch': prog_mis,
        'late_telecast': late,
        'not_aired':     not_aired,
        'no_mapping':    no_mapping,
        'extra':         extra,
        'planned':       planned,
        'max_data_date': str(max_maponline_date) if max_maponline_date else None,
    }
