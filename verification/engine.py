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
    # Only MediaWatch rows are used here; MapOnline rows are matched independently
    # by run_maponline_scope() so neither pool steals rows from the other.
    lmrb_qs = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id,
        source='mediawatch',
    )
    # Always exclude manually matched LMRB rows - permanently locked once a
    # ManualMatch record exists.  This filter applies in both smart and reset modes.
    lmrb_qs = lmrb_qs.filter(is_manual_matched=False)
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


def run_maponline_scope(account_id, channel, month, mode='smart'):
    """
    Preliminary matching: match ScheduleRows against MapOnline LMRBRows only.

    Uses separate lock fields (is_maponline_matched / is_maponline_schedule_matched)
    so this run never interferes with the authoritative LMRB (MediaWatch) engine.

    mode='smart':  Only process rows not yet MapOnline-matched.
    mode='reset':  Clear all MapOnline match fields for the scope, then re-run.

    Returns dict: {'matched': int, 'not_matched': int}
    """
    active_schedules = _active_schedules_for_scope(account_id, channel, month)
    makeup_schedules = _makeup_schedules_for_scope(active_schedules)

    # Date range
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

    # Cap at latest MapOnline date available
    max_maponline_date = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id, source='maponline',
    ).aggregate(d=Max('date'))['d']
    if max_maponline_date:
        date_end = min(date_end, max_maponline_date) if date_end else max_maponline_date
    max_verify_ts = pd.Timestamp(date_end) if date_end else None

    # All scope ScheduleRow IDs (for reset)
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

    if mode == 'reset':
        # Clear MapOnline lock on previously matched LMRBRows
        LMRBRow.objects.filter(
            maponline_schedule_matches__id__in=scope_sch_ids,
        ).update(is_maponline_schedule_matched=False)
        # Clear MapOnline match fields on ScheduleRows
        ScheduleRow.objects.filter(id__in=scope_sch_ids).update(
            is_maponline_matched=False,
            matched_maponline_lmrb=None,
            maponline_matched_at=None,
        )

    if not scope_sch_ids:
        return {'matched': 0, 'not_matched': 0}

    # Build MapOnline LMRB pool (source='maponline' rows only)
    lmrb_qs = LMRBRow.objects.filter(
        _lmrb_channel_q(channel), account_id=account_id,
        source='maponline',
        is_manual_matched=False,
        is_maponline_schedule_matched=False,
    )
    if date_start:
        lmrb_qs = lmrb_qs.filter(date__gte=date_start)
    if date_end:
        lmrb_qs = lmrb_qs.filter(date__lte=date_end)

    mon_pool = _build_mon_pool(lmrb_qs)
    brand_theme_map = _build_maponline_brand_theme_map(account_id)

    if mon_pool.empty or not brand_theme_map:
        return {'matched': 0, 'not_matched': len(scope_sch_ids)}

    all_matched_dfs = []
    global_consumed_idx: set = set()

    schedules_to_run = active_schedules if active_schedules else [None]
    schedules_to_run = schedules_to_run + makeup_schedules

    for sched in schedules_to_run:
        if sched is not None:
            sch_qs = ScheduleRow.objects.filter(
                schedule=sched, ad_type='COMMERCIAL BENEFITS',
            )
        else:
            sch_qs = ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type='COMMERCIAL BENEFITS',
            )

        sch_qs = sch_qs.filter(is_manual_matched=False)
        if mode == 'smart':
            sch_qs = sch_qs.filter(is_maponline_matched=False)

        sch_df = _build_sch_df(sch_qs)
        if sch_df.empty:
            continue

        m_df, _pm, _l, _na, _extra, consumed = match_ads(
            sch_df, mon_pool, brand_theme_map,
            pre_matched_idx=global_consumed_idx,
            max_verify_date=max_verify_ts,
        )
        global_consumed_idx = consumed
        if not m_df.empty:
            all_matched_dfs.append(m_df)

    # Collect matched pairs and lock with MapOnline-specific fields
    now = timezone.now()
    sch_updates  = []
    lmrb_updates = []
    matched_count = 0

    for df in all_matched_dfs:
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            sch_id  = row.get('_sch_row_id')
            lmrb_id = row.get('_lmrb_row_id')
            if sch_id and lmrb_id:
                sch_updates.append(ScheduleRow(
                    id=sch_id,
                    is_maponline_matched=True,
                    matched_maponline_lmrb_id=lmrb_id,
                    maponline_matched_at=now,
                ))
                lmrb_updates.append(LMRBRow(
                    id=lmrb_id,
                    is_maponline_schedule_matched=True,
                ))
                matched_count += 1

    if sch_updates:
        ScheduleRow.objects.bulk_update(
            sch_updates,
            ['is_maponline_matched', 'matched_maponline_lmrb_id', 'maponline_matched_at'],
        )
    if lmrb_updates:
        LMRBRow.objects.bulk_update(lmrb_updates, ['is_maponline_schedule_matched'])

    not_matched = len(scope_sch_ids) - matched_count
    return {'matched': matched_count, 'not_matched': not_matched}


def auto_run_maponline_for_account(account_id):
    """
    Trigger smart MapOnline preliminary matching for every (channel × month) scope
    that has both ScheduleRow records AND MapOnline LMRBRow records.

    Per-scope errors are caught and logged.
    Returns list of {'channel', 'month', 'ok', ...} dicts.
    """
    sch_channels = list(
        ScheduleRow.objects.filter(account_id=account_id)
        .values_list('channel', flat=True).distinct()
    )
    maponline_channels_lower = {
        c.lower(): c
        for c in LMRBRow.objects.filter(account_id=account_id, source='maponline')
                                 .values_list('channel', flat=True).distinct()
    }
    overlap = [ch for ch in sch_channels if ch.lower() in maponline_channels_lower]

    results = []
    for channel in sorted(set(overlap)):
        months = list(
            ScheduleRow.objects.filter(account_id=account_id, channel=channel)
            .values_list('month', flat=True).distinct()
        )
        for month in months:
            try:
                result = run_maponline_scope(account_id, channel, month, mode='smart')
                results.append({'channel': channel, 'month': month, 'ok': True, **result})
            except Exception as exc:
                logger.warning(
                    'auto_run_maponline_for_account: %s/%s failed: %s', channel, month, exc,
                )
                results.append({
                    'channel': channel, 'month': month,
                    'ok': False, 'error': str(exc),
                })
    return results
