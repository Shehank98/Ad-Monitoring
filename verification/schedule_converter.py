"""
Schedule Excel converter.

Converts the "pivot" agency schedule format (rows = programme/time-slots,
columns = dates, cell values = number of spots) into the flat ScheduleRow
format expected by the upload pipeline.

Works for all channels — no channel-specific logic.

Flat output columns
-------------------
    Advertisement_Type  — 'COMMERCIAL BENEFITS' or 'SPONSORSHIP BENEFITS'
    Programme           — programme name
    Date                — 'YYYY-MM-DD' string
    Day                 — day abbreviation (MON, TUE, …)
    Start_Time          — 'HH:MM:SS'
    End_Time            — 'HH:MM:SS'
    Duration            — integer seconds
    Brand               — brand / copy title
    Spot_Number         — 1-based spot index within that date

If the file is already in flat format (no detectable header row or no date
columns), `convert_schedule_excel` returns None so the caller can fall back
to a plain pd.read_excel().
"""

import re
from datetime import datetime

import numpy as np
import pandas as pd


# ── Helpers ───────────────────────────────────────────────────────────────────

_SECTION_COMMERCIAL  = 'COMMERCIAL BENEFITS'
_SECTION_SPONSORSHIP = 'SPONSORSHIP BENEFITS'

_SKIP_KEYWORDS = (
    'TOTAL', 'VAT', 'SSCL', 'SUMMARY', 'ROI', 'CPRP',
    'COMMERCIAL ONLY', 'ALL EXPO', 'NOTE:', 'GRAND TOTAL',
)


def _should_skip(val: str) -> bool:
    v = val.strip().upper()
    if not v or v == 'NAN':
        return True
    return any(kw in v for kw in _SKIP_KEYWORDS)


def _normalise_section(val: str):
    """Return canonical section name or None if not a section marker."""
    v = val.strip().upper()
    if 'SPONSORSHIP' in v:
        return _SECTION_SPONSORSHIP
    if 'COMMERCIAL' in v or 'BONUS' in v:
        return _SECTION_COMMERCIAL
    return None


def _convert_time(val) -> str:
    """Convert any Excel time representation to 'HH:MM:SS'."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return ''
    try:
        if isinstance(val, str):
            return val.strip()
        if isinstance(val, datetime):
            return val.strftime('%H:%M:%S')
        if hasattr(val, 'total_seconds'):          # timedelta
            ts = int(val.total_seconds())
            return f'{ts // 3600:02d}:{(ts % 3600) // 60:02d}:{ts % 60:02d}'
        if isinstance(val, (int, float)):           # Excel decimal fraction
            ts = round(float(val) * 86400)
            return f'{ts // 3600:02d}:{(ts % 3600) // 60:02d}:{ts % 60:02d}'
    except Exception:
        pass
    return str(val)


# ── Core detection ────────────────────────────────────────────────────────────

def _detect_header_row(df: pd.DataFrame):
    """
    Scan first 15 rows for a cell containing 'PROGRAM' or 'PROGRAMME'.
    Returns (row_index, programme_col_index) or (None, None).
    """
    for r in range(min(15, len(df))):
        for c in range(min(6, len(df.columns))):
            val = str(df.iloc[r, c]).strip().upper()
            if val in ('PROGRAM', 'PROGRAMME'):
                return r, c
    return None, None


def _build_col_map(df: pd.DataFrame, header_row: int, prog_col: int) -> dict:
    """
    Walk the header row and map semantic names → column indices.
    end_time = col immediately after TIME header (auto-detected).
    """
    col_map = {'programme': prog_col}
    for c in range(len(df.columns)):
        val = str(df.iloc[header_row, c]).strip().upper()
        if val == 'DAY':
            col_map['day'] = c
        elif val == 'TIME':
            col_map['start_time'] = c
            col_map['end_time'] = c + 1
        elif val == 'DUR':
            col_map['duration'] = c
        elif val == 'BRAND':
            col_map['brand'] = c
    return col_map


def _find_date_columns(df: pd.DataFrame, header_row: int, col_map: dict) -> dict:
    """
    Return {col_index: 'YYYY-MM-DD'} for all date-valued cells in the header row
    that come after the last known text-header column.
    """
    min_col = max(col_map.values()) + 1
    date_cols = {}
    for c in range(min_col, len(df.columns)):
        val = df.iloc[header_row, c]
        if hasattr(val, 'year') and hasattr(val, 'month') and hasattr(val, 'day'):
            try:
                date_cols[c] = pd.Timestamp(val).strftime('%Y-%m-%d')
            except Exception:
                pass
    return date_cols


# ── Public API ────────────────────────────────────────────────────────────────

def convert_schedule_excel(file_obj) -> pd.DataFrame | None:
    """
    Convert a pivot-format agency schedule Excel into a flat DataFrame.

    Parameters
    ----------
    file_obj : file-like or path
        The uploaded Excel file.

    Returns
    -------
    pd.DataFrame with columns:
        Advertisement_Type, Programme, Date, Day,
        Start_Time, End_Time, Duration, Brand, Spot_Number
    Or None if the file is not in pivot format (caller should fall back to
    plain pd.read_excel()).
    """
    # Read with header=None so we can detect the header row ourselves
    try:
        raw = pd.read_excel(file_obj, header=None)
    except Exception:
        return None

    header_row, prog_col = _detect_header_row(raw)
    if header_row is None:
        return None                 # not pivot format

    col_map   = _build_col_map(raw, header_row, prog_col)
    date_cols = _find_date_columns(raw, header_row, col_map)

    if not date_cols:
        return None                 # no date columns → not pivot format

    required = ['day', 'start_time', 'duration']
    if any(k not in col_map for k in required):
        return None                 # essential columns missing

    records = []
    current_section = ''
    current_brand   = ''

    for r in range(header_row + 1, len(raw)):
        try:
            c1_raw = raw.iloc[r, col_map['programme']]
            c1_str = str(c1_raw).strip()

            if _should_skip(c1_str):
                continue

            # Section header?
            section = _normalise_section(c1_str)
            if section:
                current_section = section
                current_brand   = ''
                continue

            # Determine if this is a data row or a brand/sub-header row
            day_val  = raw.iloc[r, col_map['day']]
            time_val = raw.iloc[r, col_map['start_time']]
            has_day  = not (pd.isna(day_val)  or str(day_val).strip() == '')
            has_time = not (pd.isna(time_val) or str(time_val).strip() == '')

            if has_day and has_time:
                # ── Data row ──────────────────────────────────────────────
                programme = c1_str
                day       = str(day_val).strip()
                start_t   = _convert_time(time_val)
                end_t     = _convert_time(raw.iloc[r, col_map['end_time']]) \
                            if 'end_time' in col_map else ''
                try:
                    dur = int(float(raw.iloc[r, col_map['duration']]))
                except (ValueError, TypeError):
                    dur = 0

                # Brand: prefer explicit brand column, fall back to last header
                brand = current_brand
                if 'brand' in col_map:
                    b_raw = raw.iloc[r, col_map['brand']]
                    if not pd.isna(b_raw) and str(b_raw).strip():
                        brand = str(b_raw).strip()

                # Expand each date column into individual spot rows
                for c_idx, date_str in date_cols.items():
                    spots_raw = raw.iloc[r, c_idx]
                    try:
                        spots = int(float(spots_raw)) if not pd.isna(spots_raw) else 0
                    except (ValueError, TypeError):
                        spots = 0
                    for spot_num in range(1, spots + 1):
                        records.append({
                            'Advertisement_Type': current_section,
                            'Programme':          programme,
                            'Date':               date_str,
                            'Day':                day,
                            'Start_Time':         start_t,
                            'End_Time':           end_t,
                            'Duration':           dur,
                            'Brand':              brand,
                            'Spot_Number':        spot_num,
                        })
            else:
                # ── Brand / sub-header row ────────────────────────────────
                current_brand = c1_str

        except Exception:
            continue

    if not records:
        return None

    df = pd.DataFrame(records)
    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    df.sort_values(['Date', 'Advertisement_Type', 'Programme', 'Spot_Number'], inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df
