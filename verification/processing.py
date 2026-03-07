"""
Core ad-matching engine.

Five-category output: Matched, Programme Mismatch, Late Telecast, Not Aired, Extra Aired.

Matching algorithm (two passes):
  Pass 1 — for each schedule row, find LMRB candidates by:
    1. Brand → Theme lookup (respecting optional duration on the mapping)
    2. Exact calendar date match
    3. Exact duration match (if both sides have it)
    4. Time-window: advt_time (or prog_time) must fall within planned Start_Time–End_Time
       (Rule 3: Prog_time is the primary signal; falls back to Advt_time)
    5a. In-window candidate found + programme name word overlap ≥ 50% → MATCHED
    5b. In-window candidate found + programme name word overlap < 50% → PROGRAMME MISMATCH
        (ad aired within the planned slot but in a different programme)
    — Candidates that exist only OUTSIDE the time window are NOT consumed in Pass 1.
      The schedule row proceeds to Pass 2 so Late Telecast / Not Aired can be assigned.

  Pass 2 — for schedule rows still unmatched after Pass 1:
    6. Same brand+theme+duration on a LATER date (Rule 7: aired_date > sch_date) → LATE TELECAST
    7. No candidate anywhere → NOT AIRED

  Programme name matching uses word-overlap scoring: score = (matching words) / (scheduled
  programme words). Score ≥ 0.5 → MATCHED; score < 0.5 → PROGRAMME MISMATCH.

Locking: any LMRB row consumed by Matched/Programme Mismatch/Late Telecast is
locked (cannot be reused), preventing double-counting.
Extra Aired: LMRB rows not consumed by any schedule row.

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
    """
    entries = brand_theme_map.get(norm_brand, [])
    themes = []
    for norm_theme, map_dur in entries:
        if map_dur is None:
            themes.append(norm_theme)
        elif sch_dur is not None and int(sch_dur) == int(map_dur):
            themes.append(norm_theme)
    return list(dict.fromkeys(themes))  # deduplicate, preserve order


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

    brand_theme_map: {norm_brand: [(norm_theme, duration_or_None), ...]}

    pre_matched_fp:
        Set of LMRB fingerprints already locked in a previous run.
        Those monitoring rows will be treated as consumed from the start.

    skip_sch_keys:
        Set of (norm_brand, date, start_time, end_time, dur) tuples for schedule
        rows that already have a 'matched' MatchResult — skip them in smart re-run.

    pre_matched_idx:
        Set of mon_pool integer indices already consumed by a previous schedule
        run.  Used for multi-schedule processing on the same shared LMRB pool.

    max_verify_date:
        pandas Timestamp (or None).  Schedule rows whose date is strictly after
        this value are skipped — LMRB data is not yet available for those dates.
        They will be processed automatically when new monitoring data is uploaded.

    Returns (matched_df, prog_mismatch_df, late_telecast_df, not_aired_df,
             extra_df, consumed_idx).
    consumed_idx is the complete set of mon_pool integer indices consumed by
    this call (includes pre_matched_idx entries).
    """
    if pre_matched_fp is None:
        pre_matched_fp = set()
    if skip_sch_keys is None:
        skip_sch_keys = set()

    # Initialise consumed index set from both pre-consumed sources
    matched_idx: set = set()
    if pre_matched_idx:
        matched_idx.update(pre_matched_idx)
    if pre_matched_fp:
        for idx, row in mon_pool.iterrows():
            if lmrb_fingerprint(row) in pre_matched_fp:
                matched_idx.add(idx)

    matched_rows   = []
    prog_mis_rows  = []
    late_rows      = []
    not_aired_rows = []
    unmatched_sched = []

    # Determine which time column the pool has for programme-start matching
    # Rule 3: prefer Prog_time (programme start time); fall back to Advt_time
    pool_has_prog_secs = '_prog_secs' in mon_pool.columns
    pool_has_air_secs  = '_air_secs'  in mon_pool.columns

    # ── PASS 1 ────────────────────────────────────────────────────────────────
    for _, row in sch_df.iterrows():
        sch_date       = row.get('Date')
        sch_brand_norm = row.get('_norm_brand', '')
        sch_brand      = row.get('Brand', sch_brand_norm)
        sch_dur        = row.get('_dur')
        sch_start      = row.get('_start_secs')
        sch_end        = row.get('_end_secs')
        sch_programme  = row.get('Programme', '')
        start_time_str = str(row.get('Start_Time', ''))
        end_time_str   = str(row.get('End_Time', ''))
        dur_val        = row.get('Duration', sch_dur)

        # Smart re-run: skip rows already matched in a prior run
        sch_key = (sch_brand_norm, str(sch_date), start_time_str, end_time_str, str(sch_dur))
        if sch_key in skip_sch_keys:
            continue

        # Rule 8: skip rows beyond the latest available LMRB data date
        if max_verify_date is not None and sch_date is not None:
            try:
                if pd.notna(sch_date) and sch_date > max_verify_date:
                    continue
            except TypeError:
                pass

        themes = _get_themes_for_dur(brand_theme_map, sch_brand_norm, sch_dur)
        if not themes:
            not_aired_rows.append({
                'Brand':          sch_brand,
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Start_Time':     start_time_str,
                'End_Time':       end_time_str,
                'Duration':       dur_val,
                'Status':         'No Brand Mapping',
                '_lmrb_fp':       '',
                '_sch_row_id':    row.get('_sch_db_id'),
            })
            continue

        theme_mask = mon_pool['_norm_theme'].isin(themes)
        date_mask  = mon_pool['Date'] == sch_date
        used_mask  = ~mon_pool.index.isin(matched_idx)
        candidates = mon_pool[theme_mask & date_mask & used_mask]

        if pd.notna(sch_dur) and 'Dur' in candidates.columns:
            candidates = candidates[candidates['Dur'] == sch_dur]

        if candidates.empty:
            unmatched_sched.append(row)
            continue

        # ── Time-window split ────────────────────────────────────────────────
        # Rule 3: use Prog_time (programme start time) for window check.
        # Fall back to Advt_time when Prog_time is absent or all-null.
        time_col = None
        if pool_has_prog_secs and candidates['_prog_secs'].notna().any():
            time_col = '_prog_secs'
        elif pool_has_air_secs and candidates['_air_secs'].notna().any():
            time_col = '_air_secs'

        # ── Time-window: only in-window candidates qualify for Pass 1 ──────────
        # Rule (corrected):
        #   MATCHED          — advt_time within [start, end] AND programme name matches
        #   PROGRAMME MISMATCH — advt_time within [start, end] BUT programme name differs
        #   Outside window on same day → NOT consumed here; schedule row goes to Pass 2
        if sch_start is not None and sch_end is not None and time_col:
            in_window = (candidates[time_col] >= sch_start) & (candidates[time_col] <= sch_end)
            cands_in  = candidates[in_window]
        else:
            # No time-window info available — treat all same-day candidates as in-window
            cands_in = candidates

        if not cands_in.empty:
            # Pick best candidate by programme-name word overlap (highest score wins
            # when multiple in-window rows exist for the same theme+duration).
            has_prog_col = 'Program' in cands_in.columns
            if has_prog_col and sch_programme:
                overlap_scores = cands_in['Program'].apply(
                    lambda p: _word_overlap_score(sch_programme, p)
                )
                best_idx   = overlap_scores.idxmax()
                prog_score = float(overlap_scores[best_idx])
            else:
                best_idx   = cands_in.index[0]
                prog_score = 1.0  # no programme column or no planned name → treat as match

            matched_idx.add(best_idx)
            mon_row = mon_pool.loc[best_idx]
            fp = lmrb_fingerprint(mon_row)
            base_record = {
                'Brand':          sch_brand,
                'Theme':          mon_row.get('Advt_Theme', ''),
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Aired_Date':     mon_row.get('Date', ''),
                'Planned_Start':  start_time_str,
                'Planned_End':    end_time_str,
                'Air_Time':       mon_row.get('Advt_time', ''),
                'Duration':       mon_row.get('Dur', sch_dur),
                'Source':         mon_row.get('_source', ''),
                '_lmrb_fp':       fp,
                '_sch_key':       sch_key,
                '_sch_row_id':    row.get('_sch_db_id'),
                '_lmrb_row_id':   mon_row.get('_lmrb_db_id'),
            }

            # ≥ 50 % of planned programme words match → full match
            # < 50 % word overlap → programme mismatch (aired in wrong programme)
            if prog_score >= 0.5:
                base_record['Status'] = 'Matched'
                matched_rows.append(base_record)
            else:
                base_record['Status'] = 'Programme Mismatch'
                prog_mis_rows.append(base_record)

        else:
            # No in-window candidate on this date.
            # Out-of-window same-day rows are intentionally left unconsumed so the
            # schedule row can be reconsidered in Pass 2 (Late Telecast / Not Aired).
            unmatched_sched.append(row)

    # ── PASS 2: Late Telecast / Not Aired ─────────────────────────────────────
    for row in unmatched_sched:
        sch_date       = row.get('Date')
        sch_brand_norm = row.get('_norm_brand', '')
        sch_brand      = row.get('Brand', sch_brand_norm)
        sch_dur        = row.get('_dur')
        sch_programme  = row.get('Programme', '')
        start_time_str = str(row.get('Start_Time', ''))
        end_time_str   = str(row.get('End_Time', ''))
        dur_val        = row.get('Duration', sch_dur)
        sch_key        = (sch_brand_norm, str(sch_date), start_time_str, end_time_str, str(sch_dur))

        themes = _get_themes_for_dur(brand_theme_map, sch_brand_norm, sch_dur)

        theme_mask = mon_pool['_norm_theme'].isin(themes)
        used_mask  = ~mon_pool.index.isin(matched_idx)
        candidates = mon_pool[theme_mask & used_mask]

        if pd.notna(sch_dur) and 'Dur' in candidates.columns:
            candidates = candidates[candidates['Dur'] == sch_dur]

        # Rule 7: ad can only be a Late Telecast if it aired AFTER the scheduled date.
        # Aired date strictly before the scheduled date is invalid (not a late airing).
        # (Same-date is already handled in Pass 1; here we require Date > sch_date.)
        if sch_date is not None and pd.notna(sch_date):
            candidates = candidates[candidates['Date'] > sch_date]
        else:
            candidates = candidates[candidates['Date'] != sch_date]

        if not candidates.empty:
            best_idx = candidates.index[0]
            matched_idx.add(best_idx)
            mon_row = mon_pool.loc[best_idx]
            fp = lmrb_fingerprint(mon_row)
            late_rows.append({
                'Brand':          sch_brand,
                'Theme':          mon_row.get('Advt_Theme', ''),
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Aired_Date':     mon_row.get('Date', ''),
                'Planned_Start':  start_time_str,
                'Planned_End':    end_time_str,
                'Air_Time':       mon_row.get('Advt_time', ''),
                'Duration':       mon_row.get('Dur', sch_dur),
                'Source':         mon_row.get('_source', ''),
                'Status':         'Late Telecast',
                '_lmrb_fp':       fp,
                '_sch_key':       sch_key,
                '_sch_row_id':    row.get('_sch_db_id'),
                '_lmrb_row_id':   mon_row.get('_lmrb_db_id'),
            })
        else:
            not_aired_rows.append({
                'Brand':          sch_brand,
                'Programme':      sch_programme,
                'Scheduled_Date': sch_date,
                'Start_Time':     start_time_str,
                'End_Time':       end_time_str,
                'Duration':       dur_val,
                'Status':         'Not Aired',
                '_lmrb_fp':       '',
                '_sch_key':       sch_key,
                '_sch_row_id':    row.get('_sch_db_id'),
            })

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
        matched_idx,          # 6th value — full set of consumed pool indices
    )
