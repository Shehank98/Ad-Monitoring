"""
Schedule Excel converter.

Converts the "pivot" agency schedule format (rows = programme/time-slots,
columns = dates, cell values = number of spots) into the flat ScheduleRow
format expected by the upload pipeline.

Works for all channels - no channel-specific logic.

Flat output columns
-------------------
    Advertisement_Type  - 'COMMERCIAL BENEFITS' or 'SPONSORSHIP BENEFITS'
    Programme           - programme name
    Date                - 'YYYY-MM-DD' string
    Day                 - day abbreviation (MON, TUE, …)
    Start_Time          - 'HH:MM:SS'
    End_Time            - 'HH:MM:SS'
    Duration            - integer seconds
    Brand               - brand / copy title
    Spot_Number         - 1-based spot index within that date

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

def find_flat_header_row(file_obj, sheet_name=0) -> int:
    """
    Scan first 20 rows for the row containing ≥2 schedule-header keywords.
    Returns the 0-based row index to use as header= in pd.read_excel().
    Returns 0 if not found (preserves current behaviour).
    """
    _KEYWORDS = {'date', 'brand', 'programme', 'program', 'duration',
                 'time', 'advertisement', 'advertisement_type'}
    try:
        raw = pd.read_excel(file_obj, header=None, nrows=20, sheet_name=sheet_name)
    except Exception:
        return 0
    for r in range(len(raw)):
        row_vals = [str(v).lower().strip() for v in raw.iloc[r] if pd.notna(v) and str(v).strip()]
        matches = sum(1 for rv in row_vals if any(kw in rv for kw in _KEYWORDS))
        if matches >= 2:
            return r
    return 0


def list_excel_sheets(file_obj) -> list:
    """Return all sheet names in the workbook."""
    try:
        xl = pd.ExcelFile(file_obj)
        return xl.sheet_names
    except Exception:
        return []


def detect_sheet_metadata(file_obj, sheet_name) -> dict:
    """
    Run pivot converter (or flat fallback) on one sheet.
    Returns {sheet_name, month, start_date, end_date, row_count,
             commercial_spots, sponsorship_spots, total_spots,
             is_pivot, is_schedule, schedule_number}.
    """
    _SUMMARY_NAMES = {
        'cover', 'channel summary', 'day wise', 'spot summary',
        'summary', 'rate card', 'index', 'contents',
    }
    result = {
        'sheet_name':       sheet_name,
        'month':            '',
        'start_date':       '',
        'end_date':         '',
        'row_count':        0,
        'commercial_spots': 0,
        'sponsorship_spots':0,
        'total_spots':      0,
        'is_pivot':         False,
        'is_schedule':      False,
        'schedule_number':  '',
    }
    try:
        # ── Step 1: read metadata rows (schedule number + fallback month) ──────
        raw_meta = None
        try:
            raw_meta = pd.read_excel(file_obj, header=None, nrows=15, sheet_name=sheet_name)
            file_obj.seek(0)
        except Exception:
            pass

        # Schedule number: scan first 15 rows × first 8 columns
        if raw_meta is not None:
            _SCH_KEYWORDS = {
                'sch no', 'sch_no', 'sch.no', 'sch no.', 'sch. no',
                'schedule no', 'schedule number', 'sch num', 'schedule #',
            }
            for r in range(len(raw_meta)):
                for c in range(min(8, len(raw_meta.columns))):
                    cell = str(raw_meta.iloc[r, c]).lower().strip()
                    if (any(kw in cell for kw in _SCH_KEYWORDS)
                            or (cell.startswith('sch') and len(cell) < 15)):
                        for nc in range(c + 1, len(raw_meta.columns)):
                            val = raw_meta.iloc[r, nc]
                            s = str(val).strip()
                            if pd.notna(val) and s and s.lower() != 'nan':
                                result['schedule_number'] = s
                                break
                        break
                if result['schedule_number']:
                    break

        # ── Step 2: try pivot conversion ───────────────────────────────────────
        df = convert_schedule_excel(file_obj, sheet_name=sheet_name)
        file_obj.seek(0)
        pivot = df is not None and not df.empty
        if not pivot:
            header_row = find_flat_header_row(file_obj, sheet_name=sheet_name)
            file_obj.seek(0)
            try:
                df = pd.read_excel(file_obj, header=header_row, sheet_name=sheet_name)
                df.columns = df.columns.str.strip()
                file_obj.seek(0)
            except Exception:
                return result

        if df is None or df.empty:
            return result

        result['row_count'] = len(df)
        result['is_pivot']  = pivot

        # ── Step 3: month / date detection ────────────────────────────────────
        # Level 1: case-insensitive 'Date' column in converted df
        date_col = next((c for c in df.columns if c.lower().strip() == 'date'), None)
        if date_col:
            dates = pd.to_datetime(df[date_col], errors='coerce').dropna()
            if not dates.empty:
                result['start_date'] = str(dates.min().date())
                result['end_date']   = str(dates.max().date())
                result['month']      = dates.min().strftime('%B %Y')

        # Level 2: scan metadata rows for "Revised Date" / "Period" / "Month" label
        if not result['month'] and raw_meta is not None:
            _DATE_LABEL_KW = {'revised date', 'period', 'month', 'campaign period', 'campaign month'}
            for r in range(len(raw_meta)):
                for c in range(min(5, len(raw_meta.columns))):
                    cell = str(raw_meta.iloc[r, c]).lower().strip()
                    if any(kw in cell for kw in _DATE_LABEL_KW):
                        for nc in range(c + 1, len(raw_meta.columns)):
                            val = raw_meta.iloc[r, nc]
                            if pd.notna(val) and str(val).strip():
                                dt = pd.to_datetime(str(val), dayfirst=True, errors='coerce')
                                if pd.notna(dt):
                                    result['month'] = dt.strftime('%B %Y')
                                    break
                        break
                if result['month']:
                    break

        # ── Step 4: commercial / sponsorship spot counts ───────────────────────
        if 'Advertisement_Type' in df.columns:
            at = df['Advertisement_Type'].astype(str).str.strip().str.upper()
            c_spots = int((at == 'COMMERCIAL BENEFITS').sum())
            s_spots = int(at.isin(['SPONSORSHIP BENEFITS', 'SPONSORSHIP']).sum())
            result['commercial_spots']  = c_spots
            result['sponsorship_spots'] = s_spots
            result['total_spots']       = c_spots + s_spots

        # ── Step 5: is_schedule heuristic ─────────────────────────────────────
        name_lower = str(sheet_name).lower().strip()
        result['is_schedule'] = (
            result['total_spots'] > 0
            and not any(kw in name_lower for kw in _SUMMARY_NAMES)
        )

    except Exception:
        pass
    return result


def convert_schedule_excel(file_obj, sheet_name=0) -> pd.DataFrame | None:
    """
    Convert a pivot-format agency schedule Excel into a flat DataFrame.

    Parameters
    ----------
    file_obj : file-like or path
        The uploaded Excel file.
    sheet_name : int or str
        Sheet to read (default 0 = first sheet).

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
        raw = pd.read_excel(file_obj, header=None, sheet_name=sheet_name)
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

            # Determine if this is a data row or a brand/sub-header row.
            # Check day/time FIRST so that brand names containing 'BONUS' or
            # 'COMMERCIAL' (e.g. "Bonus Promo", "Commercial Deal") are never
            # misidentified as section headers.
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
                # If brand is still empty, use the programme name as brand.
                # This handles formats (e.g. ITN sponsorship) where each data
                # row's first column IS the brand/slot type (BB, Opening/Closing,
                # Trailers) with no separate brand sub-header row.
                if not brand:
                    brand = programme

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
                # Not a data row - check for section header first, then brand
                section = _normalise_section(c1_str)
                if section:
                    current_section = section
                    current_brand   = ''
                else:
                    # ── Brand / sub-header row ────────────────────────────
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
