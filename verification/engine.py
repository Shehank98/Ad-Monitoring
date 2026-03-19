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
from django.db.models import Max, Min
from django.utils import timezone

from core.models import Account, BrandMapping, LMRBRow, MatchResult, MonitoringData, Schedule, ScheduleRow
from .processing import (
    _parse_time_to_seconds, lmrb_fingerprint, match_ads, normalize,
)

logger = logging.getLogger(__name__)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_brand_theme_map(account_id):
    brand_theme_map = {}
    for bm in BrandMapping.objects.filter(account_id=account_id):
        norm_brand  = normalize(bm.brand)
        norm_theme  = normalize(bm.theme)
        mapping_dur = int(bm.duration) if bm.duration is not None else None
        brand_theme_map.setdefault(norm_brand, []).append((norm_theme, mapping_dur))
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
        'prog_time', 'program', 'is_sponsorship_matched',
    ))
    if not records:
        return pd.DataFrame(columns=[
            '_lmrb_db_id', 'Advt_Theme', 'Date', 'Advt_time',
            'Dur', '_source', '_norm_theme', '_air_secs',
            'Prog_time', 'Program', '_prog_secs', '_is_spon_matched',
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
    }, inplace=True)
    df['Date']        = pd.to_datetime(df['Date'], errors='coerce')
    df['_norm_theme'] = df['Advt_Theme'].apply(normalize)
    df['_air_secs']   = df['Advt_time'].apply(_parse_time_to_seconds)
    df['Dur']         = pd.to_numeric(df['Dur'], errors='coerce')
    # Rule 3: programme start time for time-window matching
    df['_prog_secs']  = df['Prog_time'].apply(_parse_time_to_seconds)
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
        account_id=account_id, channel__iexact=channel,
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

    # ── Build shared LMRB pool ─────────────────────────────────────────────────
    # Case-insensitive channel match so "Sirasa TV" / "SIRASA TV" resolve to the
    # same pool regardless of how the LMRB file spells the channel name.
    lmrb_qs = LMRBRow.objects.filter(account_id=account_id, channel__iexact=channel)
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

    # ── Rule 11: Extra Airings - pool rows not consumed by any schedule ────────
    # Only COMMERCIAL spots within the schedule period are shown as extra.
    # Rows already claimed by the sponsorship engine (_is_spon_matched=True)
    # are excluded - they belong to the sponsorship reconciliation, not here.
    extra_rows = []
    for idx, mon_row in mon_pool.iterrows():
        if idx not in global_consumed_idx:
            if mon_row.get('_is_spon_matched', False):
                continue  # already claimed by sponsorship engine
            extra_rows.append({
                'Theme':    mon_row.get('Advt_Theme', ''),
                'Date':     mon_row.get('Date', ''),
                'Air_Time': mon_row.get('Advt_time', ''),
                'Duration': mon_row.get('Dur', ''),
                'Source':   mon_row.get('_source', ''),
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

    # ── Persist MatchResult records ────────────────────────────────────────────
    account_obj = Account.objects.get(pk=account_id)
    _persist_results(account_obj, channel, month, [
        (matched_df,   'matched'),
        (prog_mis_df,  'programme_mismatch'),
        (late_df,      'late_telecast'),
        (not_aired_df, 'not_aired'),
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
