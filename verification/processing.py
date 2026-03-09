"""
Core ad-matching engine.

Five-category output: Matched, Programme Mismatch, Late Telecast, Not Aired, Extra Aired.

Matching algorithm (four passes — all schedule spots complete each pass before the next):

  Pass 1 — Exact Match
    For every schedule row find LMRB candidates with:
      • same date  • same theme (via BrandMapping)  • same duration
      • advt_time falls within planned [start_time, end_time] window
    If found → MATCHED.  Lock both records.

  Pass 2 — Programme Mismatch (only rows still unmatched after Pass 1)
    Candidates must satisfy:
      • same date  • same theme  • same duration
      • advt_time is AFTER end_time  • (advt_time − end_time) ≤ 10 minutes (600s)
    If found → PROGRAMME MISMATCH.  Lock both records.

  Pass 3 — Late Telecast (only rows still unmatched after Pass 2)
    Candidates must satisfy:
      • aired_date > scheduled date  • same theme  • same duration
      (no time-window constraint — ad can air at any time on a later date)
    If found → LATE TELECAST.  Lock both records.

  Pass 4 — Not Aired
    Any schedule row still unmatched → NOT AIRED.

Processing rules
  • All schedule rows complete Pass 1 before any row enters Pass 2.
  • Locked records (is_matched=True) cannot be claimed by a later pass or row.
  • Matching occurs within the same channel (enforced by the caller).

brand_theme_map format supplied by the caller:
  {norm_brand: [(norm_theme, duration_or_None), ...]}

Smart re-run / multi-schedule support:
  pre_matched_fp  — set of LMRB fingerprints locked in a previous run.
  skip_sch_keys   — set of (brand_norm, date, start_time, end_time, dur) tuples
                    for schedule rows already matched in a previous run.
  pre_matched_idx — set of mon_pool integer indices already consumed by a
                    previous schedule run (used for multi-schedule processing
                    on the same shared LMRB pool).
  max_verify_date — pandas Timestamp; schedule rows whose date exceeds this
                    are skipped entirely (no LMRB data available yet for those
                    dates — they will be processed when new data is uploaded).
"""
import hashlib
import pandas as pd


def normalize(text) -> str:
    return str(text).lower().strip() if not pd.isna(text) else ''


def lmrb_fingerprint(row: pd.Series) -> str:
    """16-char hash that uniquely identifies an LMRB monitoring row.

    Used to lock matched rows between smart re-runs so the same monitoring
    entry cannot be matched twice across multiple verification runs.
    """
    key = (
        f"{row.get('Advt_Theme', '')}|"
        f"{row.get('Date', '')}|"
        f"{row.get('Advt_time', '')}|"
        f"{row.get('Dur', '')}|"
        f"{row.get('_source', '')}"
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


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


def prepare_monitoring_pool(
    mon_files,
    channel_filter=None,
    date_start=None,
    date_end=None,
) -> pd.DataFrame:
    """
    Combine LMRB + MapOnline DataFrames into a single pool.

    mon_files: list of (data_type, df) where data_type is 'maponline' or 'mediawatch'.

    channel_filter: if set, only keep rows where the Channel column matches exactly.
                    This is critical when a single LMRB file contains multiple channels.
    date_start/date_end: optional date objects; rows outside this range are dropped.
                    Use the schedule's auto-detected start/end dates to avoid matching
                    LMRB rows that belong to a completely different schedule period.
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

    pool = pd.concat(parts, ignore_index=True)

    # ── Channel filter (critical for multi-channel LMRB files) ────────────────
    if channel_filter and 'Channel' in pool.columns:
        pool = pool[pool['Channel'].astype(str).str.strip() == channel_filter.strip()]

    # ── Date range filter (schedule period) ───────────────────────────────────
    if date_start is not None and 'Date' in pool.columns:
        pool = pool[pool['Date'] >= pd.Timestamp(date_start)]
    if date_end is not None and 'Date' in pool.columns:
        pool = pool[pool['Date'] <= pd.Timestamp(date_end)]

    return pool.reset_index(drop=True)


def prepare_schedule(df: pd.DataFrame) -> pd.DataFrame:
    """
    Prepare the schedule DataFrame:
    - Keep only COMMERCIAL BENEFITS rows.
    - Parse Date, Start_Time, End_Time, Duration.
    """
    df = df.copy()
    df.columns = df.columns.str.strip()

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

    Themes may end with '*' (wildcard) — see _build_theme_mask().
    """
    entries = brand_theme_map.get(norm_brand, [])
    themes = []
    for norm_theme, map_dur in entries:
        if map_dur is None:
            themes.append(norm_theme)
        elif sch_dur is not None and int(sch_dur) == int(map_dur):
            themes.append(norm_theme)
    return list(dict.fromkeys(themes))  # deduplicate, preserve order


def _build_theme_mask(pool: pd.DataFrame, themes: list) -> pd.Series:
    """
    Build a boolean mask for pool rows whose '_norm_theme' matches any entry in themes.

    Supports wildcard '*' suffix in theme values:
      - 'ai national expo 2025*'  matches any LMRB theme that starts with
        'ai national expo 2025' (e.g. 'ai national expo 2025_3 (30)(sin)').
      - Themes without '*' require an exact (case-insensitive) match.

    This lets a single BrandMapping row like 'Ai National Expo 2025*' cover
    all campaign variants (_1, _2, _3 …) without needing a separate mapping
    for every theme name the LMRB data uses.
    """
    if pool.empty:
        return pd.Series(False, index=pool.index)

    exact_themes = [t for t in themes if not t.endswith('*')]
    wildcard_prefixes = [t[:-1] for t in themes if t.endswith('*')]

    mask = pool['_norm_theme'].isin(exact_themes)
    for prefix in wildcard_prefixes:
        mask = mask | pool['_norm_theme'].str.startswith(prefix, na=False)
    return mask


def _fuzzy_score(sch_prog: str, lmrb_prog) -> int:
    """Return fuzzywuzzy token_sort_ratio between two programme name strings."""
    try:
        from fuzzywuzzy import fuzz  # optional dependency
        return fuzz.token_sort_ratio(normalize(sch_prog), normalize(str(lmrb_prog) if lmrb_prog else ''))
    except Exception:
        return 0


def _word_overlap_score(sch_prog: str, lmrb_prog) -> float:
    """
    Word-overlap score: fraction of scheduled programme words present in the
    LMRB programme name.

    Examples:
      'Sirasa Superstar' vs 'Sirasa Superstar' → 1.0  (all words match → Matched)
      'Super Show Colombo' vs 'Super Show'     → 0.67 (2/3 match → Matched)
      'Sirasa Superstar' vs 'Sirasa News'      → 0.5  (1/2 match → borderline)
      'Sirasa Superstar' vs 'Rupavahini Drama' → 0.0  (no words → Mismatch)

    Returns 1.0 when sch_prog is empty (nothing to compare → treat as match).
    """
    sch_words  = set(normalize(sch_prog).split()) if sch_prog else set()
    lmrb_words = set(normalize(str(lmrb_prog) if lmrb_prog else '').split())
    if not sch_words:
        return 1.0  # no planned programme → cannot mismatch
    return len(sch_words & lmrb_words) / len(sch_words)


_PROG_MISMATCH_TOLERANCE = 600   # 10 minutes in seconds


def match_ads(
    sch_df: pd.DataFrame,
    mon_pool: pd.DataFrame,
    brand_theme_map: dict,
    pre_matched_fp: set = None,
    skip_sch_keys: set = None,
    pre_matched_idx: set = None,
    max_verify_date=None,
):
    """
    Match schedule rows against monitoring pool using brand mapping.

    Four-pass algorithm — ALL schedule rows complete each pass before any row
    enters the next pass (strict ordering: Exact Match → Programme Mismatch →
    Late Telecast → Not Aired).

    brand_theme_map: {norm_brand: [(norm_theme, duration_or_None), ...]}

    pre_matched_fp:
        Set of LMRB fingerprints already locked in a previous run.
    skip_sch_keys:
        Set of (norm_brand, date, start_time, end_time, dur) tuples for schedule
        rows already matched in a prior smart re-run — skip them here.
    pre_matched_idx:
        Set of mon_pool integer indices already consumed by a previous schedule
        run (multi-schedule shared-pool support).
    max_verify_date:
        pandas Timestamp (or None).  Schedule rows whose date is strictly after
        this value are skipped — no LMRB data available yet.

    Returns (matched_df, prog_mismatch_df, late_telecast_df, not_aired_df,
             extra_df, consumed_idx).
    consumed_idx is the full set of mon_pool integer indices consumed by this call.
    """
    if pre_matched_fp is None:
        pre_matched_fp = set()
    if skip_sch_keys is None:
        skip_sch_keys = set()

    # ── Seed consumed-index set from prior runs ───────────────────────────────
    matched_idx: set = set()
    if pre_matched_idx:
        matched_idx.update(pre_matched_idx)
    if pre_matched_fp:
        for idx, row in mon_pool.iterrows():
            if lmrb_fingerprint(row) in pre_matched_fp:
                matched_idx.add(idx)

    pool_has_air_secs = '_air_secs' in mon_pool.columns

    # ── Helper: build a consistent result record ──────────────────────────────
    def _make_record(sch_row, mon_row, status, sch_key):
        fp = lmrb_fingerprint(mon_row)
        return {
            'Brand':          sch_row.get('Brand', sch_row.get('_norm_brand', '')),
            'Theme':          mon_row.get('Advt_Theme', ''),
            'Programme':      sch_row.get('Programme', ''),
            'Scheduled_Date': sch_row.get('Date'),
            'Aired_Date':     mon_row.get('Date', ''),
            'Planned_Start':  str(sch_row.get('Start_Time', '')),
            'Planned_End':    str(sch_row.get('End_Time', '')),
            'Air_Time':       mon_row.get('Advt_time', ''),
            'Duration':       mon_row.get('Dur', sch_row.get('_dur')),
            'Source':         mon_row.get('_source', ''),
            'Status':         status,
            '_lmrb_fp':       fp,
            '_sch_key':       sch_key,
            '_sch_row_id':    sch_row.get('_sch_db_id'),
            '_lmrb_row_id':   mon_row.get('_lmrb_db_id'),
        }

    # ── Pre-process schedule rows: skip/no-mapping/date-cap checks ────────────
    # Each entry: (sch_series, sch_key, themes)
    # Rows with no brand mapping or beyond max_verify_date go straight to not_aired.
    active_rows   = []   # rows that will enter the passes
    not_aired_rows = []

    for _, row in sch_df.iterrows():
        sch_date       = row.get('Date')
        sch_brand_norm = row.get('_norm_brand', '')
        sch_dur        = row.get('_dur')
        start_time_str = str(row.get('Start_Time', ''))
        end_time_str   = str(row.get('End_Time', ''))

        sch_key = (sch_brand_norm, str(sch_date), start_time_str, end_time_str, str(sch_dur))
        if sch_key in skip_sch_keys:
            continue

        if max_verify_date is not None and sch_date is not None:
            try:
                if pd.notna(sch_date) and sch_date > max_verify_date:
                    continue
            except TypeError:
                pass

        themes = _get_themes_for_dur(brand_theme_map, sch_brand_norm, sch_dur)
        if not themes:
            not_aired_rows.append({
                'Brand':          row.get('Brand', sch_brand_norm),
                'Programme':      row.get('Programme', ''),
                'Scheduled_Date': sch_date,
                'Start_Time':     start_time_str,
                'End_Time':       end_time_str,
                'Duration':       row.get('Duration', sch_dur),
                'Status':         'No Brand Mapping',
                '_lmrb_fp':       '',
                '_sch_row_id':    row.get('_sch_db_id'),
            })
            continue

        active_rows.append((row, sch_key, themes))

    # ── Helper: get candidates (theme + duration filtered, not consumed) ───────
    def _candidates(themes, extra_mask=None):
        theme_mask = _build_theme_mask(mon_pool, themes)
        used_mask  = ~mon_pool.index.isin(matched_idx)
        mask = theme_mask & used_mask
        if extra_mask is not None:
            mask = mask & extra_mask
        cands = mon_pool[mask]
        return cands

    def _dur_filter(cands, sch_dur):
        if pd.notna(sch_dur) and 'Dur' in cands.columns:
            return cands[cands['Dur'] == sch_dur]
        return cands

    # ═══════════════════════════════════════════════════════════════════════════
    # PASS 1 — Exact Match
    #   • Same date  • In-window  • Theme  • Duration
    # ═══════════════════════════════════════════════════════════════════════════
    matched_rows      = []
    after_pass1: list = []   # rows that did not match in Pass 1

    for row, sch_key, themes in active_rows:
        sch_date  = row.get('Date')
        sch_dur   = row.get('_dur')
        sch_start = row.get('_start_secs')
        sch_end   = row.get('_end_secs')

        date_mask = mon_pool['Date'] == sch_date
        cands     = _dur_filter(_candidates(themes, date_mask), sch_dur)

        if cands.empty:
            after_pass1.append((row, sch_key, themes))
            continue

        # In-window: advt_time within [start_time, end_time]
        if sch_start is not None and sch_end is not None and pool_has_air_secs:
            in_win = cands[
                (cands['_air_secs'] >= sch_start) & (cands['_air_secs'] <= sch_end)
            ]
        else:
            in_win = cands   # no time info → treat all same-day as in-window

        if in_win.empty:
            after_pass1.append((row, sch_key, themes))
            continue

        best_idx = in_win.index[0]
        matched_idx.add(best_idx)
        matched_rows.append(_make_record(row, mon_pool.loc[best_idx], 'Matched', sch_key))

    # ═══════════════════════════════════════════════════════════════════════════
    # PASS 2 — Programme Mismatch
    #   • Same date  • advt_time AFTER end_time  • Within 10 minutes  • Theme  • Duration
    # ═══════════════════════════════════════════════════════════════════════════
    prog_mis_rows = []
    after_pass2: list = []

    for row, sch_key, themes in after_pass1:
        sch_date  = row.get('Date')
        sch_dur   = row.get('_dur')
        sch_end   = row.get('_end_secs')

        date_mask = mon_pool['Date'] == sch_date if sch_date is not None and pd.notna(sch_date) else (mon_pool['Date'] != mon_pool['Date'])
        cands     = _dur_filter(_candidates(themes, date_mask), sch_dur)

        if cands.empty or not pool_has_air_secs or sch_end is None:
            after_pass2.append((row, sch_key, themes))
            continue

        # After end_time, within 10-minute tolerance
        after_window = cands[
            (cands['_air_secs'] > sch_end) &
            (cands['_air_secs'] - sch_end <= _PROG_MISMATCH_TOLERANCE)
        ]

        if after_window.empty:
            after_pass2.append((row, sch_key, themes))
            continue

        # Pick the closest one (smallest overshoot)
        best_idx = (after_window['_air_secs'] - sch_end).idxmin()
        matched_idx.add(best_idx)
        prog_mis_rows.append(
            _make_record(row, mon_pool.loc[best_idx], 'Programme Mismatch', sch_key)
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # PASS 3 — Late Telecast
    #   • aired_date > scheduled_date  • Theme  • Duration  (any time)
    # ═══════════════════════════════════════════════════════════════════════════
    late_rows      = []

    for row, sch_key, themes in after_pass2:
        sch_date = row.get('Date')
        sch_dur  = row.get('_dur')

        if sch_date is None or pd.isna(sch_date):
            not_aired_rows.append({
                'Brand':          row.get('Brand', row.get('_norm_brand', '')),
                'Programme':      row.get('Programme', ''),
                'Scheduled_Date': sch_date,
                'Start_Time':     str(row.get('Start_Time', '')),
                'End_Time':       str(row.get('End_Time', '')),
                'Duration':       row.get('Duration', sch_dur),
                'Status':         'Not Aired',
                '_lmrb_fp':       '',
                '_sch_key':       sch_key,
                '_sch_row_id':    row.get('_sch_db_id'),
            })
            continue

        later_mask = mon_pool['Date'] > sch_date
        cands      = _dur_filter(_candidates(themes, later_mask), sch_dur)

        if cands.empty:
            not_aired_rows.append({
                'Brand':          row.get('Brand', row.get('_norm_brand', '')),
                'Programme':      row.get('Programme', ''),
                'Scheduled_Date': sch_date,
                'Start_Time':     str(row.get('Start_Time', '')),
                'End_Time':       str(row.get('End_Time', '')),
                'Duration':       row.get('Duration', sch_dur),
                'Status':         'Not Aired',
                '_lmrb_fp':       '',
                '_sch_key':       sch_key,
                '_sch_row_id':    row.get('_sch_db_id'),
            })
            continue

        # Pick the earliest available date
        best_idx = cands['Date'].idxmin()
        matched_idx.add(best_idx)
        late_rows.append(
            _make_record(row, mon_pool.loc[best_idx], 'Late Telecast', sch_key)
        )

    # ── Extra aired ────────────────────────────────────────────────────────────
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
        matched_idx,
    )
