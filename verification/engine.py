"""
Verification engine — callable from views and upload handlers.

Functions
---------
run_scope(account_id, channel, month, mode='smart')
    Loads Excel files, runs the two-pass matching algorithm, persists
    MatchResult records.  Returns
    (matched_df, prog_mismatch_df, late_df, not_aired_df, extra_df), total_sch.

auto_run_all_for_account(account_id)
    Finds every (channel, month) combination that has BOTH a Schedule AND
    MonitoringData for the account, then calls run_scope(..., mode='smart') for
    each.  Per-scope errors are caught and logged — a single failure never
    blocks other scopes.  Returns a list of result dicts.
"""
import logging

import pandas as pd
from django.db.models import Max, Min

from core.models import Account, BrandMapping, MatchResult, MonitoringData, Schedule
from .processing import (
    lmrb_fingerprint, match_ads, normalize,
    prepare_monitoring_pool, prepare_schedule,
)

logger = logging.getLogger(__name__)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_brand_theme_map(account_id):
    """Build the brand→[(theme, duration)] map from BrandMapping records."""
    brand_theme_map = {}
    for bm in BrandMapping.objects.filter(account_id=account_id):
        norm_brand  = normalize(bm.brand)
        norm_theme  = normalize(bm.theme)
        mapping_dur = int(bm.duration) if bm.duration is not None else None
        brand_theme_map.setdefault(norm_brand, []).append((norm_theme, mapping_dur))
    return brand_theme_map


def _to_date(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        ts = pd.Timestamp(val)
        return ts.date() if not pd.isna(ts) else None
    except Exception:
        return None


def _str(val):
    return '' if (val is None or (isinstance(val, float) and pd.isna(val))) else str(val)


def _int(val):
    try:
        return int(val) if val is not None and not pd.isna(val) else None
    except Exception:
        return None


def _persist_results(account_obj, channel, month, results_by_status):
    """Bulk-create MatchResult rows from the five engine output DataFrames."""
    STATUS_MAP = {
        'matched':            'matched',
        'programme mismatch': 'programme_mismatch',
        'late telecast':      'late_telecast',
        'not aired':          'not_aired',
        'no brand mapping':   'no_mapping',
    }
    to_save = []
    for df, default_status in results_by_status:
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            raw_status = _str(r.get('Status', default_status))
            db_status  = STATUS_MAP.get(raw_status.lower(), default_status)
            to_save.append(MatchResult(
                account          = account_obj,
                channel          = channel,
                month            = month,
                brand            = _str(r.get('Brand', '')),
                programme        = _str(r.get('Programme', '')),
                scheduled_date   = _to_date(r.get('Scheduled_Date')),
                planned_start    = _str(r.get('Planned_Start', r.get('Start_Time', ''))),
                planned_end      = _str(r.get('Planned_End', r.get('End_Time', ''))),
                duration         = _int(r.get('Duration')),
                theme            = _str(r.get('Theme', '')),
                aired_date       = _to_date(r.get('Aired_Date')),
                air_time         = _str(r.get('Air_Time', '')),
                source           = _str(r.get('Source', '')),
                status           = db_status,
                lmrb_fingerprint = _str(r.get('_lmrb_fp', '')),
            ))
    if to_save:
        MatchResult.objects.bulk_create(to_save, batch_size=500)


# ── Public API ─────────────────────────────────────────────────────────────────

def run_scope(account_id, channel, month, mode='smart'):
    """
    Load files, run the matching engine, persist MatchResult records.

    mode='smart': existing Matched results are kept; only new / previously
                  unmatched rows are processed.  LMRB rows consumed by
                  previous runs are pre-locked via their fingerprints.
    mode='reset': all prior MatchResults for this scope are deleted first,
                  then a full fresh run is performed.

    Returns
    -------
    (matched_df, prog_mismatch_df, late_df, not_aired_df, extra_df), total_sch
    """
    schedules = Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
    mon_qs    = MonitoringData.objects.filter(account_id=account_id, channel=channel)

    if not schedules.exists():
        raise ValueError(f'No schedule found for "{channel}" / "{month}".')
    if not mon_qs.exists():
        raise ValueError(f'No monitoring data found for channel "{channel}".')

    # ── Load schedule ──────────────────────────────────────────────────────────
    sch_frames = []
    for s in schedules:
        df = pd.read_excel(s.file.path)
        df.columns = df.columns.str.strip()
        sch_frames.append(df)
    sch_df = pd.concat(sch_frames, ignore_index=True)
    sch_df = prepare_schedule(sch_df)

    # Date range from schedule (for LMRB pool filtering)
    sch_dates  = schedules.aggregate(min=Min('start_date'), max=Max('end_date'))
    date_start = sch_dates['min']
    date_end   = sch_dates['max']

    # ── Load monitoring pool ───────────────────────────────────────────────────
    mon_files = []
    for m in mon_qs:
        df = pd.read_excel(m.file.path)
        df.columns = df.columns.str.strip()
        mon_files.append((m.data_type, df))

    mon_pool = prepare_monitoring_pool(
        mon_files,
        channel_filter=channel,
        date_start=date_start,
        date_end=date_end,
    )

    brand_theme_map = _build_brand_theme_map(account_id)

    # ── Smart / reset mode ────────────────────────────────────────────────────
    pre_matched_fp = set()
    skip_sch_keys  = set()
    scope_qs = MatchResult.objects.filter(account_id=account_id, channel=channel, month=month)

    if mode == 'reset':
        scope_qs.delete()
    else:  # smart
        for mr in scope_qs:
            if mr.lmrb_fingerprint:
                pre_matched_fp.add(mr.lmrb_fingerprint)
            if mr.status == 'matched':
                skip_sch_keys.add((
                    normalize(mr.brand),
                    str(mr.scheduled_date),
                    mr.planned_start,
                    mr.planned_end,
                    str(mr.duration),
                ))

    # ── Run engine ────────────────────────────────────────────────────────────
    matched_df, prog_mis_df, late_df, not_aired_df, extra_df = match_ads(
        sch_df, mon_pool, brand_theme_map,
        pre_matched_fp=pre_matched_fp,
        skip_sch_keys=skip_sch_keys,
    )

    # ── Persist ───────────────────────────────────────────────────────────────
    account_obj = Account.objects.get(pk=account_id)
    _persist_results(account_obj, channel, month, [
        (matched_df,   'matched'),
        (prog_mis_df,  'programme_mismatch'),
        (late_df,      'late_telecast'),
        (not_aired_df, 'not_aired'),
    ])

    return (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), len(sch_df)


def auto_run_all_for_account(account_id):
    """
    Trigger smart verification for every (channel, month) scope that has BOTH a
    Schedule AND MonitoringData for the given account.

    Per-scope errors are caught and logged — a single failure never prevents
    other scopes from running or the caller from completing.

    Returns
    -------
    list of dicts: [{'channel': ..., 'month': ..., 'ok': bool, 'error': str}, ...]
    """
    sch_channels = set(
        Schedule.objects.filter(account_id=account_id)
        .values_list('channel', flat=True)
    )
    mon_channels = set(
        MonitoringData.objects.filter(account_id=account_id)
        .values_list('channel', flat=True)
    )
    overlap = sch_channels & mon_channels

    results = []
    for channel in sorted(overlap):
        months = list(
            Schedule.objects.filter(account_id=account_id, channel=channel)
            .values_list('month', flat=True)
            .distinct()
        )
        for month in months:
            try:
                run_scope(account_id, channel, month, mode='smart')
                results.append({'channel': channel, 'month': month, 'ok': True})
            except Exception as exc:
                logger.warning(
                    'auto_run_all_for_account: scope %s/%s failed: %s',
                    channel, month, exc,
                )
                results.append({
                    'channel': channel, 'month': month,
                    'ok': False, 'error': str(exc),
                })

    return results
