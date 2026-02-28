"""
Core ad-matching engine – ported from original Streamlit app.
Pure Python / pandas, no Streamlit dependencies.
"""
import pandas as pd
from datetime import timedelta


def normalize(text) -> str:
    return str(text).lower().strip() if not pd.isna(text) else ''


def _prepare_maponline(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = df.columns.str.strip()
    rename = {}
    if 'Theme'    in df.columns and 'Advt_Theme' not in df.columns: rename['Theme']    = 'Advt_Theme'
    if 'Prg Date' in df.columns and 'Date'       not in df.columns: rename['Prg Date'] = 'Date'
    if 'Prg Name' in df.columns and 'Program'    not in df.columns: rename['Prg Name'] = 'Program'
    if 'Ad Dur'   in df.columns and 'Dur'         not in df.columns: rename['Ad Dur']   = 'Dur'
    df.rename(columns=rename, inplace=True)
    return df


def _prepare_mediawatch(df: pd.DataFrame) -> pd.DataFrame:
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


def prepare_dataframes(mon_df, sch_df, data_source,
                       mon_date_col, sch_date_col,
                       mon_theme_col, sch_theme_col,
                       mon_prog_col,  sch_prog_col,
                       mon_dur_col,   sch_dur_col):
    """Rename columns to internal names and parse dates."""
    if data_source == 'maponline':
        mon_df = _prepare_maponline(mon_df)
    else:
        mon_df = _prepare_mediawatch(mon_df)

    mon_df.rename(columns={mon_theme_col: 'Advt_Theme',
                             mon_prog_col:  'Program',
                             mon_dur_col:   'Dur'}, inplace=True, errors='ignore')
    sch_df.rename(columns={sch_theme_col:  'Advt_Theme',
                             sch_prog_col:   'Program',
                             sch_dur_col:    'Dur'}, inplace=True, errors='ignore')

    mon_df[mon_date_col] = pd.to_datetime(mon_df[mon_date_col], errors='coerce')
    sch_df[sch_date_col] = pd.to_datetime(sch_df[sch_date_col], errors='coerce')
    mon_df['Date']       = mon_df[mon_date_col]
    sch_df['Date']       = sch_df[sch_date_col]

    if 'Advt_Theme' in mon_df.columns:
        mon_df['_norm_theme'] = mon_df['Advt_Theme'].apply(normalize)
    if 'Advt_Theme' in sch_df.columns:
        sch_df['_norm_theme'] = sch_df['Advt_Theme'].apply(normalize)

    return mon_df, sch_df


def match_ads(sch_df, mon_df, theme_mapping, date_tolerance=0):
    """
    Match schedule rows against monitoring data.
    Returns four lists of dicts: matched, shifted, unmatched, extra.
    """
    # Build lookup: (norm_sch_theme, sch_dur|None) → (norm_mon_theme, mon_dur|None)
    lookup = {}
    for m in theme_mapping:
        k = (normalize(m['schedule_theme']), m.get('schedule_duration'))
        lookup[k] = (normalize(m['map_online_theme']), m.get('map_duration'))

    matched_idx = set()
    matched, shifted, unmatched, extra = [], [], [], []

    for sch_row in sch_df.itertuples():
        sch_date    = sch_row.Date
        sch_theme   = normalize(getattr(sch_row, '_norm_theme', getattr(sch_row, 'Advt_Theme', '')))
        sch_dur     = getattr(sch_row, 'Dur', None)
        sch_program = getattr(sch_row, 'Program', '')

        # Resolve theme mapping
        key = (sch_theme, sch_dur)
        if key not in lookup:
            key = (sch_theme, None)
        if key not in lookup:
            unmatched.append({'Scheduled_Date': sch_date, 'Theme': getattr(sch_row, 'Advt_Theme', ''),
                               'Program': sch_program, 'Duration': sch_dur, 'Reason': 'No Theme Mapping'})
            continue

        mon_theme, mon_dur = lookup[key]
        date_min = sch_date - timedelta(days=date_tolerance)
        date_max = sch_date + timedelta(days=date_tolerance)

        # Filter by theme
        theme_mask = mon_df['_norm_theme'] == mon_theme
        tm = mon_df[theme_mask]
        if mon_dur is not None and 'Dur' in tm.columns:
            tm = tm[tm['Dur'] == mon_dur]

        # Match within date window
        in_window = tm[(tm['Date'] >= date_min) & (tm['Date'] <= date_max)]
        in_window = in_window[~in_window.index.isin(matched_idx)]

        if not in_window.empty:
            best = in_window.index[0]
            matched_idx.add(best)
            row = mon_df.loc[best]
            matched.append({'Scheduled_Date': sch_date, 'Aired_Date': row['Date'],
                             'Program': row.get('Program', ''), 'Map_Theme': row.get('Advt_Theme', ''),
                             'Duration': row.get('Dur', sch_dur), 'Status': 'Aired as Scheduled'})
        else:
            # Check other dates (shifted)
            other = tm[~tm.index.isin(matched_idx) &
                       ~((tm['Date'] >= date_min) & (tm['Date'] <= date_max))]
            if not other.empty:
                best = other.index[0]
                matched_idx.add(best)
                row = mon_df.loc[best]
                shifted.append({'Scheduled_Date': sch_date, 'Aired_Date': row['Date'],
                                 'Program': row.get('Program', ''), 'Map_Theme': row.get('Advt_Theme', ''),
                                 'Duration': row.get('Dur', sch_dur),
                                 'Days_Difference': (row['Date'] - sch_date).days})
            else:
                unmatched.append({'Scheduled_Date': sch_date, 'Theme': getattr(sch_row, 'Advt_Theme', ''),
                                   'Program': sch_program, 'Duration': sch_dur, 'Reason': 'Not Aired'})

    # Extra aired
    for idx, row in mon_df.iterrows():
        if idx not in matched_idx:
            extra.append({'Date': row['Date'], 'Program': row.get('Program', ''),
                          'Map_Theme': row.get('Advt_Theme', ''), 'Duration': row.get('Dur', '')})

    return (
        pd.DataFrame(matched),
        pd.DataFrame(shifted),
        pd.DataFrame(unmatched),
        pd.DataFrame(extra),
    )
