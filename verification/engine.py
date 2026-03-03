"""
Verification engine — queries ScheduleRow and LMRBRow from the database.

Row-level locking:
  When a ScheduleRow is matched to an LMRBRow, both have is_matched=True set.
  Smart re-run queries only rows where is_matched=False, so matched pairs are
  never reprocessed or double-counted.

Reset mode:
  Unlocks all rows for the scope (is_matched=False), deletes MatchResult
  records, then runs a full fresh match.

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
    df['_dur']        = pd.to_numeric(df['Duration'], errors='coerce')
    return df.reset_index(drop=True)


def _build_mon_pool(lmrb_qs):
    """Convert a LMRBRow queryset to a monitoring pool DataFrame."""
    records = list(lmrb_qs.values(
        'id', 'advt_theme', 'date', 'advt_time', 'duration', 'source',
    ))
    if not records:
        return pd.DataFrame(columns=[
            '_lmrb_db_id', 'Advt_Theme', 'Date', 'Advt_time',
            'Dur', '_source', '_norm_theme', '_air_secs',
        ])
    df = pd.DataFrame(records)
    df.rename(columns={
        'id':         '_lmrb_db_id',
        'advt_theme': 'Advt_Theme',
        'date':       'Date',
        'advt_time':  'Advt_time',
        'duration':   'Dur',
        'source':     '_source',
    }, inplace=True)
    df['Date']        = pd.to_datetime(df['Date'], errors='coerce')
    df['_norm_theme'] = df['Advt_Theme'].apply(normalize)
    df['_air_secs']   = df['Advt_time'].apply(_parse_time_to_seconds)
    df['Dur']         = pd.to_numeric(df['Dur'], errors='coerce')
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


# ── Public API ─────────────────────────────────────────────────────────────────

def run_scope(account_id, channel, month, mode='smart'):
    """
    Load ScheduleRow + LMRBRow records from DB, run matching, persist results.

    mode='smart':  Query only is_matched=False rows.  Rows locked in previous
                   runs are skipped automatically (no fingerprint tracking needed).
    mode='reset':  Unlock all rows for this scope, delete MatchResults, then
                   run a full fresh match.

    Returns (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), total_sch.
    """
    # ── Date range from Schedule records (for LMRB date filter) ───────────────
    schedules = Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
    sch_dates  = schedules.aggregate(min=Min('start_date'), max=Max('end_date'))
    date_start = sch_dates['min']
    date_end   = sch_dates['max']
    # Fallback: if Schedule header has no dates, derive range from actual ScheduleRow dates.
    # This guarantees the LMRB pool is always restricted to the schedule period, preventing
    # LMRB data from other months (e.g. September) contaminating a March schedule run.
    if not date_start or not date_end:
        row_dates  = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).aggregate(min=Min('date'), max=Max('date'))
        date_start = date_start or row_dates['min']
        date_end   = date_end   or row_dates['max']

    # ── Reset mode: unlock all rows for this scope ─────────────────────────────
    if mode == 'reset':
        scope_sch_ids = list(
            ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type='COMMERCIAL BENEFITS',
            ).values_list('id', flat=True)
        )
        LMRBRow.objects.filter(matched_schedule_id__in=scope_sch_ids).update(
            is_matched=False, matched_schedule=None, matched_at=None,
        )
        ScheduleRow.objects.filter(id__in=scope_sch_ids).update(
            is_matched=False, matched_lmrb=None, matched_at=None,
        )
        MatchResult.objects.filter(account_id=account_id, channel=channel, month=month).delete()

    # ── Query ScheduleRows ────────────────────────────────────────────────────
    sch_qs = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month,
        ad_type='COMMERCIAL BENEFITS',
    )
    if mode == 'smart':
        sch_qs = sch_qs.filter(is_matched=False)

    total_sch = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month,
        ad_type='COMMERCIAL BENEFITS',
    ).count()

    if total_sch == 0:
        raise ValueError(
            f'No schedule rows found for "{channel}" / "{month}". '
            'Please re-upload the schedule file so rows are parsed into the database.'
        )

    sch_df = _build_sch_df(sch_qs)

    # If smart mode and all rows are already matched, return empty results
    if sch_df.empty and mode == 'smart':
        empty = pd.DataFrame()
        return (empty, empty, empty, empty, empty), total_sch

    # ── Query LMRBRows ─────────────────────────────────────────────────────────
    lmrb_qs = LMRBRow.objects.filter(account_id=account_id, channel=channel)
    if mode == 'smart':
        lmrb_qs = lmrb_qs.filter(is_matched=False)
    if date_start:
        lmrb_qs = lmrb_qs.filter(date__gte=date_start)
    if date_end:
        lmrb_qs = lmrb_qs.filter(date__lte=date_end)

    mon_pool = _build_mon_pool(lmrb_qs)

    brand_theme_map = _build_brand_theme_map(account_id)

    # ── Run matching ──────────────────────────────────────────────────────────
    matched_df, prog_mis_df, late_df, not_aired_df, extra_df = match_ads(
        sch_df, mon_pool, brand_theme_map,
    )

    # ── Lock matched rows in DB ────────────────────────────────────────────────
    _lock_matched_rows(matched_df, prog_mis_df, late_df)

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

    Per-scope errors are caught — a single failure never blocks other scopes.
    Returns list of {'channel', 'month', 'ok', 'error'} dicts.
    """
    sch_channels = set(
        ScheduleRow.objects.filter(account_id=account_id)
        .values_list('channel', flat=True).distinct()
    )
    lmrb_channels = set(
        LMRBRow.objects.filter(account_id=account_id)
        .values_list('channel', flat=True).distinct()
    )
    overlap = sch_channels & lmrb_channels

    results = []
    for channel in sorted(overlap):
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
