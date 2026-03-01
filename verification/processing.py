"""
Core ad-matching engine.

Five-category output: Matched, Programme Mismatch, Late Telecast, Not Aired, Extra Aired.

Matching algorithm (two passes):
  Pass 1 — for each schedule row, find LMRB candidates by:
    1. Brand → Theme lookup (respecting optional duration on the mapping)
    2. Exact calendar date match
    3. Exact duration match (if both sides have it)
    4a. Time-window match (aired time within planned Start_Time–End_Time) → MATCHED
    4b. Candidates exist but none in time window → PROGRAMME MISMATCH
       (Programme matching is done by time-window: instead of comparing programme
        names — which often differ in text — we check that the aired time falls
        within the planned slot's start/end time.)
  Pass 2 — for schedule rows still unmatched after Pass 1:
    5. Same brand+theme+duration but on a different date → LATE TELECAST
    6. No candidate anywhere → NOT AIRED

Locking: any LMRB row consumed by Matched/Programme Mismatch/Late Telecast is
locked (cannot be reused), preventing double-counting.
Extra Aired: LMRB rows not consumed by any schedule row.

brand_theme_map format supplied by the caller:
  {norm_brand: [(norm_theme, duration_or_None), ...]}
  - duration_or_None: if set, the mapping only applies to schedule rows whose
    duration equals this value; if None, the mapping applies to any duration.
"""
import pandas as pd


def normalize(text) -> str:
    return str(text).lower().strip() if not pd.isna(text) else ''


def _parse_time_to_seconds(t):
    """Convert a time value to seconds since midnight. Returns None on failure."""
    if t is None:
        return None
    try:
        if pd.isna(t):
            return None
    except (TypeError, ValueError):
        pass
    # datetime / time objects
    if hasattr(t, 'hour'):
        return t.hour * 3600 + t.minute * 60 + getattr(t, 'second', 0)
    # Excel float (fraction of a day)
    try:
        f = float(t)
        if 0.0 <= f < 1.0:
            return round(f * 86400)
    except (ValueError, TypeError):
        pass
    # String HH:MM or HH:MM:SS
    s = str(t).strip()
    parts = s.split(':')
    try:
        if len(parts) >= 2:
            h   = int(parts[0])
            m   = int(parts[1])
            sec = int(float(parts[2])) if len(parts) >= 3 else 0
            return h * 3600 + m * 60 + sec
    except (ValueError, IndexError):
        pass
    return None


def _prepare_maponline(df: pd.DataFrame) -> pd.DataFrame:
    """Rename MapOnline columns to internal standard names."""
    df = df.copy()
    df.columns = df.columns.str.strip()
    rename = {}
    if 'Theme'    in df.columns and 'Advt_Theme' not in df.columns: rename['Theme']    = 'Advt_Theme'
    if 'Prg Date' in df.columns and 'Date'       not in df.columns: rename['Prg Date'] = 'Date'
    if 'Ad Dur'   in df.columns and 'Dur'         not in df.columns: rename['Ad Dur']   = 'Dur'
    if 'Ad Start' in df.columns and 'Advt_time'   not in df.columns: rename['Ad Start'] = 'Advt_time'
    if 'Prg Name' in df.columns and 'Programme'   not in df.columns: rename['Prg Name'] = 'Programme'
    df.rename(columns=rename, inplace=True)
    return df


def _prepare_mediawatch(df: pd.DataFrame) -> pd.DataFrame:
    """Build a Date column from Dd/Mn/Yr for LMRB data."""
    df = df.copy()
    df.columns = df.columns.str.strip()
    if {'Dd', 'Mn', 'Yr'}.issubset(df.columns) and 'Date' not in df.columns:
        df['Date'] = pd.to_datetime(
            df['Yr'].astype(str) + '-' +
            df['Mn'].astype(str).str.zfill(2) + '-' +
            df['Dd'].astype(str).str.zfill(2),
            errors='coerce',
        )
    return df


def prepare_monitoring_pool(mon_files) -> pd.DataFrame:
    """
    Combine LMRB + MapOnline DataFrames into a single pool.

    mon_files: list of (data_type, df) where data_type is 'maponline' or 'mediawatch'.
    Returns a unified DataFrame with standardised columns.
    """
    parts = []
    for data_type, df in mon_files:
        if data_type == 'maponline':
            df = _prepare_maponline(df)
        else:
            df = _prepare_mediawatch(df)
        df = df.copy()
        df['_source'] = data_type
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
        if 'Dur' in df.columns:
            df['Dur'] = pd.to_numeric(df['Dur'], errors='coerce')
        if 'Advt_time' in df.columns:
            df['_air_secs'] = df['Advt_time'].apply(_parse_time_to_seconds)
        else:
            df['_air_secs'] = None
        if 'Advt_Theme' in df.columns:
            df['_norm_theme'] = df['Advt_Theme'].apply(normalize)
        else:
            df['_norm_theme'] = ''
        parts.append(df)

    if not parts:
        return pd.DataFrame(columns=['Date', 'Advt_Theme', 'Dur', '_norm_theme', '_source', '_air_secs'])
    return pd.concat(parts, ignore_index=True)


def prepare_schedule(df: pd.DataFrame) -> pd.DataFrame:
    """
    Prepare the schedule DataFrame:
    - Keep only COMMERCIAL BENEFITS rows.
    - Parse Date, Start_Time, End_Time, Duration.
    """
    df = df.copy()
    df.columns = df.columns.str.strip()

    # Keep only COMMERCIAL BENEFITS
    if 'Advertisement_Type' in df.columns:
        mask = df['Advertisement_Type'].astype(str).str.strip().str.upper() == 'COMMERCIAL BENEFITS'
        df = df[mask].copy()

    if 'Date' in df.columns:
        df['Date'] = pd.to_datetime(df['Date'], errors='coerce')

    df['_start_secs'] = df['Start_Time'].apply(_parse_time_to_seconds) if 'Start_Time' in df.columns else None
    df['_end_secs']   = df['End_Time'].apply(_parse_time_to_seconds)   if 'End_Time'   in df.columns else None
    df['_dur']        = pd.to_numeric(df['Duration'], errors='coerce') if 'Duration'   in df.columns else None
    df['_norm_brand'] = df['Brand'].apply(normalize)                   if 'Brand'      in df.columns else ''

    return df.reset_index(drop=True)


def _get_themes_for_dur(brand_theme_map, norm_brand, sch_dur):
    """
    Return the list of monitoring themes relevant for this brand + duration.

    brand_theme_map: {norm_brand: [(norm_theme, mapping_dur_or_None), ...]}
    mapping_dur_or_None: if set, only applies when sch_dur matches; if None, applies to any duration.
    """
    entries = brand_theme_map.get(norm_brand, [])
    themes = []
    for norm_theme, map_dur in entries:
        if map_dur is None:
            themes.append(norm_theme)
        elif sch_dur is not None and int(sch_dur) == int(map_dur):
            themes.append(norm_theme)
    return list(dict.fromkeys(themes))  # deduplicate, preserve order


def match_ads(sch_df: pd.DataFrame, mon_pool: pd.DataFrame, brand_theme_map: dict):
    """
    Match schedule rows against monitoring pool using brand mapping.

    brand_theme_map: {norm_brand: [(norm_theme, duration_or_None), ...]}

    Two-pass algorithm:
      Pass 1: exact date + theme + duration, then time-window → Matched / Programme Mismatch
      Pass 2: same theme + duration, different date → Late Telecast / Not Aired

    Programme validation is done via time-window (Start_Time–End_Time):
      - If aired time falls within planned slot → programme OK (Matched)
      - If aired on correct date with correct theme/duration but OUTSIDE the planned
        time slot → Programme Mismatch (still locks that LMRB row)

    Returns (matched_df, prog_mismatch_df, late_telecast_df, not_aired_df, extra_df).
    """
    matched_idx      = set()   # Locked LMRB row indices
    matched_rows     = []
    prog_mis_rows    = []
    late_rows        = []
    not_aired_rows   = []
    unmatched_sched  = []      # Schedule rows that need pass-2 checking

    # ── PASS 1 ────────────────────────────────────────────────────────────────
    for _, row in sch_df.iterrows():
        sch_date       = row.get('Date')
        sch_brand_norm = row.get('_norm_brand', '')
        sch_brand      = row.get('Brand', sch_brand_norm)
        sch_dur        = row.get('_dur')
        sch_start      = row.get('_start_secs')
        sch_end        = row.get('_end_secs')
        sch_programme  = row.get('Programme', '')

        themes = _get_themes_for_dur(brand_theme_map, sch_brand_norm, sch_dur)
        if not themes:
            not_aired_rows.append({
                'Brand':          sch_brand,
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Start_Time':     row.get('Start_Time', ''),
                'End_Time':       row.get('End_Time', ''),
                'Duration':       row.get('Duration', sch_dur),
                'Status':         'No Brand Mapping',
            })
            continue

        # Candidates: theme match + exact date + not yet used
        theme_mask = mon_pool['_norm_theme'].isin(themes)
        date_mask  = mon_pool['Date'] == sch_date
        used_mask  = ~mon_pool.index.isin(matched_idx)
        candidates = mon_pool[theme_mask & date_mask & used_mask]

        # Duration filter (exact)
        if pd.notna(sch_dur) and 'Dur' in candidates.columns:
            candidates = candidates[candidates['Dur'] == sch_dur]

        if candidates.empty:
            # Nothing on this date — will try late telecast in pass 2
            unmatched_sched.append(row)
            continue

        # Time-window split: aired time within planned Start_Time–End_Time?
        if (sch_start is not None and sch_end is not None
                and '_air_secs' in candidates.columns
                and candidates['_air_secs'].notna().any()):
            in_window  = (candidates['_air_secs'] >= sch_start) & (candidates['_air_secs'] <= sch_end)
            cands_in   = candidates[in_window]
            cands_out  = candidates[~in_window]
        else:
            cands_in  = candidates
            cands_out = pd.DataFrame()

        if not cands_in.empty:
            # ── MATCHED ───────────────────────────────────────────────────────
            best_idx = cands_in.index[0]
            matched_idx.add(best_idx)
            mon_row = mon_pool.loc[best_idx]
            matched_rows.append({
                'Brand':          sch_brand,
                'Theme':          mon_row.get('Advt_Theme', ''),
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Aired_Date':     mon_row.get('Date', ''),
                'Planned_Start':  row.get('Start_Time', ''),
                'Planned_End':    row.get('End_Time', ''),
                'Air_Time':       mon_row.get('Advt_time', ''),
                'Duration':       mon_row.get('Dur', sch_dur),
                'Source':         mon_row.get('_source', ''),
                'Status':         'Matched',
            })

        elif not cands_out.empty:
            # ── PROGRAMME MISMATCH ────────────────────────────────────────────
            # Ad found on correct date with correct theme/duration, but the aired
            # time is outside the planned programme slot.  Lock it to prevent it
            # appearing as "Extra Aired".
            best_idx = cands_out.index[0]
            matched_idx.add(best_idx)
            mon_row = mon_pool.loc[best_idx]
            prog_mis_rows.append({
                'Brand':          sch_brand,
                'Theme':          mon_row.get('Advt_Theme', ''),
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Aired_Date':     mon_row.get('Date', ''),
                'Planned_Start':  row.get('Start_Time', ''),
                'Planned_End':    row.get('End_Time', ''),
                'Air_Time':       mon_row.get('Advt_time', ''),
                'Duration':       mon_row.get('Dur', sch_dur),
                'Source':         mon_row.get('_source', ''),
                'Status':         'Programme Mismatch',
            })

    # ── PASS 2: Late Telecast / Not Aired ──────────────────────────────────────
    for row in unmatched_sched:
        sch_date       = row.get('Date')
        sch_brand_norm = row.get('_norm_brand', '')
        sch_brand      = row.get('Brand', sch_brand_norm)
        sch_dur        = row.get('_dur')
        sch_programme  = row.get('Programme', '')

        themes = _get_themes_for_dur(brand_theme_map, sch_brand_norm, sch_dur)

        theme_mask = mon_pool['_norm_theme'].isin(themes)
        used_mask  = ~mon_pool.index.isin(matched_idx)
        candidates = mon_pool[theme_mask & used_mask]

        # Duration filter
        if pd.notna(sch_dur) and 'Dur' in candidates.columns:
            candidates = candidates[candidates['Dur'] == sch_dur]

        # Exclude the original planned date (already checked in pass 1)
        candidates = candidates[candidates['Date'] != sch_date]

        if not candidates.empty:
            # ── LATE TELECAST ─────────────────────────────────────────────────
            best_idx = candidates.index[0]
            matched_idx.add(best_idx)
            mon_row = mon_pool.loc[best_idx]
            late_rows.append({
                'Brand':          sch_brand,
                'Theme':          mon_row.get('Advt_Theme', ''),
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Aired_Date':     mon_row.get('Date', ''),
                'Planned_Start':  row.get('Start_Time', ''),
                'Planned_End':    row.get('End_Time', ''),
                'Air_Time':       mon_row.get('Advt_time', ''),
                'Duration':       mon_row.get('Dur', sch_dur),
                'Source':         mon_row.get('_source', ''),
                'Status':         'Late Telecast',
            })
        else:
            # ── NOT AIRED ─────────────────────────────────────────────────────
            not_aired_rows.append({
                'Brand':          sch_brand,
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Start_Time':     row.get('Start_Time', ''),
                'End_Time':       row.get('End_Time', ''),
                'Duration':       row.get('Duration', sch_dur),
                'Status':         'Not Aired',
            })

    # ── Extra aired: LMRB rows not consumed by any schedule row ───────────────
    extra_rows = []
    for idx, mon_row in mon_pool.iterrows():
        if idx not in matched_idx:
            extra_rows.append({
                'Theme':    mon_row.get('Advt_Theme', ''),
                'Date':     mon_row.get('Date', ''),
                'Air_Time': mon_row.get('Advt_time', ''),
                'Duration': mon_row.get('Dur', ''),
                'Source':   mon_row.get('_source', ''),
            })

    def _to_df(rows):
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    return (
        _to_df(matched_rows),
        _to_df(prog_mis_rows),
        _to_df(late_rows),
        _to_df(not_aired_rows),
        _to_df(extra_rows),
    )
