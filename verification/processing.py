"""
Core ad-matching engine.

Simplified 3-category output: Matched, Not Aired, Extra Aired.
- Filters SPONSORSHIP BENEFITS from schedule (keeps COMMERCIAL BENEFITS only).
- Time-window matching: Advt_time / Ad Start must fall within Start_Time → End_Time.
- Duration: exact numeric match.
- Combined LMRB + MapOnline pool.
- BrandMapping supplied as {norm_brand: [norm_theme, ...]} dict from DB.
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


def match_ads(sch_df: pd.DataFrame, mon_pool: pd.DataFrame, brand_theme_map: dict):
    """
    Match schedule rows against monitoring pool using brand mapping.

    brand_theme_map: {norm_brand: [norm_theme1, norm_theme2, ...]}

    Matching criteria (all must pass):
      1. Theme: monitoring _norm_theme in brand's mapped themes list.
      2. Date:  exact calendar date match.
      3. Duration: exact numeric match (if both sides have it).
      4. Time window: monitoring _air_secs within [_start_secs, _end_secs] (if both sides have it).

    Returns (matched_df, not_aired_df, extra_df).
    """
    matched_idx  = set()
    matched_rows = []
    not_aired_rows = []

    for _, row in sch_df.iterrows():
        sch_date       = row.get('Date')
        sch_brand_norm = row.get('_norm_brand', '')
        sch_brand      = row.get('Brand', sch_brand_norm)
        sch_dur        = row.get('_dur')
        sch_start      = row.get('_start_secs')
        sch_end        = row.get('_end_secs')

        themes = brand_theme_map.get(sch_brand_norm, [])
        if not themes:
            not_aired_rows.append({
                'Brand':          sch_brand,
                'Scheduled_Date': sch_date,
                'Start_Time':     row.get('Start_Time', ''),
                'End_Time':       row.get('End_Time', ''),
                'Duration':       row.get('Duration', sch_dur),
                'Reason':         'No Brand Mapping',
            })
            continue

        # ── Build candidate set ───────────────────────────────────
        theme_mask = mon_pool['_norm_theme'].isin(themes)
        date_mask  = mon_pool['Date'] == sch_date
        used_mask  = ~mon_pool.index.isin(matched_idx)
        candidates = mon_pool[theme_mask & date_mask & used_mask]

        # Duration filter (exact)
        if pd.notna(sch_dur) and 'Dur' in candidates.columns:
            candidates = candidates[candidates['Dur'] == sch_dur]

        # Time-window filter
        if (sch_start is not None and sch_end is not None
                and '_air_secs' in candidates.columns
                and candidates['_air_secs'].notna().any()):
            time_ok    = (candidates['_air_secs'] >= sch_start) & (candidates['_air_secs'] <= sch_end)
            candidates = candidates[time_ok]

        if not candidates.empty:
            best_idx = candidates.index[0]
            matched_idx.add(best_idx)
            mon_row = mon_pool.loc[best_idx]
            matched_rows.append({
                'Brand':          sch_brand,
                'Theme':          mon_row.get('Advt_Theme', ''),
                'Scheduled_Date': sch_date,
                'Aired_Date':     mon_row.get('Date', ''),
                'Air_Time':       mon_row.get('Advt_time', ''),
                'Duration':       mon_row.get('Dur', sch_dur),
                'Source':         mon_row.get('_source', ''),
            })
        else:
            not_aired_rows.append({
                'Brand':          sch_brand,
                'Scheduled_Date': sch_date,
                'Start_Time':     row.get('Start_Time', ''),
                'End_Time':       row.get('End_Time', ''),
                'Duration':       row.get('Duration', sch_dur),
                'Reason':         'Not Found in Monitoring',
            })

    # Extra aired: monitoring rows not consumed by any schedule row
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

    return (
        pd.DataFrame(matched_rows)   if matched_rows   else pd.DataFrame(),
        pd.DataFrame(not_aired_rows) if not_aired_rows else pd.DataFrame(),
        pd.DataFrame(extra_rows)     if extra_rows     else pd.DataFrame(),
    )
