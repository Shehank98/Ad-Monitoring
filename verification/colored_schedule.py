"""Colored schedule pivot export.

Reads the original uploaded pivot-format Excel file and produces a new workbook
in the same layout, with date-column cells coloured by reconciliation status:

    matched / manual_match  → green  (schedule_color_aired)
    not_aired / no_mapping  → red    (schedule_color_not_aired)
    late_telecast           → purple (schedule_color_late_telecast)
    programme_mismatch      → orange (schedule_color_programme_mismatch)
    extra_aired             → blue   (schedule_color_extra_aired)
    planned (no data yet)   → grey   (schedule_color_planned)

If status_map is None, all date cells are written in the "planned" colour.

When a date cell has mixed statuses the main row shows the first status and
additional sub-rows (with blank left columns) carry the remaining statuses.

Late-telecast spots also appear in the ACTUAL aired date column (purple), even
if that column is beyond the schedule's original date range (new columns are
appended to the right).
"""

import io
import logging
from collections import defaultdict
from datetime import date as _date

logger = logging.getLogger(__name__)

# ── Sentinel row-type constants ───────────────────────────────────────────────
_ROW_DATA    = 'data'
_ROW_OTHER   = 'other'   # section header, brand header, total, blank

# ── Skip keywords (same as schedule_converter) ────────────────────────────────
_SKIP_KW = ('TOTAL', 'VAT', 'SSCL', 'SUMMARY', 'ROI', 'CPRP',
            'COMMERCIAL ONLY', 'ALL EXPO', 'NOTE:', 'GRAND TOTAL')

# Statuses that map to "aired as planned"
_AIRED_STATUSES = {'matched', 'manual_match'}
# Statuses that map to "not aired"
_NOT_AIRED_STATUSES = {'not_aired', 'no_mapping'}


def _hex_to_fill(hex_color: str):
    """Return an openpyxl PatternFill from a CSS hex string like '#22c55e'."""
    from openpyxl.styles import PatternFill
    h = hex_color.lstrip('#').upper()
    return PatternFill('solid', fgColor=h)


def _copy_cell_style(src_cell, dst_cell):
    """Copy basic style (font bold/size/color, alignment) from src to dst."""
    from copy import copy
    try:
        if src_cell.font:
            dst_cell.font = copy(src_cell.font)
        if src_cell.alignment:
            dst_cell.alignment = copy(src_cell.alignment)
        if src_cell.fill and src_cell.fill.fill_type not in (None, 'none'):
            dst_cell.fill = copy(src_cell.fill)
        if src_cell.border:
            dst_cell.border = copy(src_cell.border)
    except Exception:
        pass  # best-effort only


def _normalize(s) -> str:
    return str(s).strip().lower() if s else ''


# ── Status map builder ────────────────────────────────────────────────────────

def build_status_map(account_id, channel, month) -> dict:
    """
    Build a per-slot, per-date status count dict from MatchResult records.

    Returns
    -------
    dict keyed by (norm_programme, norm_start_time, duration_int) →
        dict keyed by date_str ('YYYY-MM-DD') →
            {
              'matched': int,
              'not_aired': int,
              'late_telecast': int,
              'programme_mismatch': int,
              'manual_match': int,
              'no_mapping': int,
              'extra_aired': int,
              'late_actual': {actual_date_str: int},  # for late spots
            }
    """
    from django.db.models import Count
    from core.models import MatchResult

    qs = (
        MatchResult.objects
        .filter(account_id=account_id, channel=channel, month=month)
        .exclude(status='extra_aired')
        .select_related('schedule_row')
        .values(
            'brand', 'programme', 'scheduled_date', 'planned_start',
            'duration', 'status', 'aired_date',
        )
    )

    status_map: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # Also track late actual dates: slot_key → {date_str → {actual_date_str → count}}
    late_actual_map: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    for row in qs:
        prog   = _normalize(row['programme'])
        start  = _normalize(row['planned_start'])
        dur    = row['duration'] or 0
        s_date = row['scheduled_date']
        status = row['status']

        if not s_date:
            continue

        date_str = s_date.strftime('%Y-%m-%d')
        slot_key = (prog, start, dur)

        # Merge 'not_aired' and 'no_mapping' under 'not_aired' bucket
        if status in _NOT_AIRED_STATUSES:
            status_map[slot_key][date_str]['not_aired'] += 1
        else:
            status_map[slot_key][date_str][status] += 1

        # Track actual aired date for late telecasts
        if status == 'late_telecast' and row['aired_date']:
            actual_str = row['aired_date'].strftime('%Y-%m-%d')
            late_actual_map[slot_key][date_str][actual_str] += 1

    # Embed late_actual into status_map
    for slot_key, date_dict in late_actual_map.items():
        for date_str, actual_dict in date_dict.items():
            status_map[slot_key][date_str]['late_actual'] = dict(actual_dict)

    return dict(status_map)


# ── Pivot structure detection (openpyxl-based) ───────────────────────────────

def _detect_pivot_openpyxl(ws):
    """
    Detect the header row and date columns in an openpyxl worksheet.

    Returns (header_row_idx, col_map, date_col_map) where:
      header_row_idx  – 1-based row index of the header row (or None)
      col_map         – {'programme': int, 'day': int, 'start_time': int,
                         'end_time': int, 'duration': int} (1-based col indices)
      date_col_map    – {col_idx: 'YYYY-MM-DD'} (1-based)
    """
    from datetime import datetime as _dt

    header_row_idx = None
    col_map = {}

    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=min(20, ws.max_row)), start=1):
        for c_idx, cell in enumerate(row[:6], start=1):
            val = str(cell.value or '').strip().upper()
            if val in ('PROGRAM', 'PROGRAMME'):
                header_row_idx = row_idx
                col_map['programme'] = c_idx
                break
        if header_row_idx:
            break

    if not header_row_idx:
        return None, {}, {}

    # Walk the header row for other semantic columns
    header_row = list(ws.iter_rows(min_row=header_row_idx, max_row=header_row_idx))[0]
    end_time_set = False
    for cell in header_row:
        val = str(cell.value or '').strip().upper()
        c   = cell.column
        if val == 'DAY':
            col_map['day'] = c
        elif val == 'TIME':
            col_map['start_time'] = c
            col_map['end_time']   = c + 1
            end_time_set = True
        elif val == 'DUR':
            col_map['duration'] = c
        elif val == 'BRAND':
            col_map['brand'] = c

    if not col_map.get('day') or not col_map.get('start_time') or not col_map.get('duration'):
        return None, {}, {}

    min_date_col = max(col_map.values()) + 1
    date_col_map = {}
    for cell in header_row:
        if cell.column < min_date_col:
            continue
        val = cell.value
        if isinstance(val, (_dt, _date)):
            try:
                if isinstance(val, _dt):
                    date_str = val.strftime('%Y-%m-%d')
                else:
                    date_str = val.strftime('%Y-%m-%d')
                date_col_map[cell.column] = date_str
            except Exception:
                pass

    return header_row_idx, col_map, date_col_map


def _is_skip_row(val_str: str) -> bool:
    v = val_str.strip().upper()
    if not v or v in ('NONE', 'NAN'):
        return True
    return any(kw in v for kw in _SKIP_KW)


def _is_data_row(ws_row, col_map) -> bool:
    """True when the row has non-empty DAY and TIME values."""
    day  = ws_row[col_map.get('day', 1) - 1].value
    time = ws_row[col_map.get('start_time', 1) - 1].value
    return bool(day and str(day).strip()) and bool(time and str(time).strip())


# ── DB-based fallback workbook builder ───────────────────────────────────────

def _build_wb_from_db(schedule, colors: dict, status_map=None):
    """
    Build a colored pivot workbook purely from ScheduleRow DB records.
    Used when the original uploaded file is no longer on disk (e.g. after Railway redeploy).
    """
    import openpyxl
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter
    from core.models import ScheduleRow
    from collections import defaultdict

    rows_qs = (
        ScheduleRow.objects
        .filter(schedule=schedule)
        .order_by('brand', 'programme', 'start_time', 'date')
    )

    fills = {
        'aired':               _hex_to_fill(colors.get('aired',               '#22c55e')),
        'not_aired':           _hex_to_fill(colors.get('not_aired',           '#ef4444')),
        'late_telecast':       _hex_to_fill(colors.get('late_telecast',       '#a855f7')),
        'programme_mismatch':  _hex_to_fill(colors.get('programme_mismatch',  '#f97316')),
        'extra_aired':         _hex_to_fill(colors.get('extra_aired',         '#3b82f6')),
        'planned':             _hex_to_fill(colors.get('planned',             '#94a3b8')),
    }
    status_to_fill = {
        'matched': fills['aired'], 'manual_match': fills['aired'],
        'not_aired': fills['not_aired'], 'late_telecast': fills['late_telecast'],
        'programme_mismatch': fills['programme_mismatch'],
        'extra_aired': fills['extra_aired'], 'planned': fills['planned'],
    }

    # Collect all unique dates sorted
    all_dates = sorted({r.date for r in rows_qs if r.date})
    if not all_dates:
        wb = Workbook()
        wb.active.cell(1, 1, 'No schedule rows found in database.')
        return wb

    # Collect any extra late-telecast actual dates
    extra_dates: set = set()
    if status_map:
        for slot_dict in status_map.values():
            for d_dict in slot_dict.values():
                for actual_date in (d_dict.get('late_actual') or {}).keys():
                    actual_d = _date.fromisoformat(actual_date) if isinstance(actual_date, str) else actual_date
                    if actual_d not in all_dates:
                        extra_dates.add(actual_date)
    sorted_extra_dates = sorted(extra_dates)

    # Group rows by (brand, programme, start_time, duration) → {date: count}
    slot_groups: dict = defaultdict(lambda: defaultdict(int))
    for r in rows_qs:
        key = (r.brand or '', r.programme or '', r.start_time or '', r.duration or 0)
        if r.date:
            slot_groups[key][r.date.strftime('%Y-%m-%d')] += 1

    wb = Workbook()
    ws = wb.active
    ws.title = 'Colored Schedule'

    # Fixed left columns: Brand, Programme, Start Time, Duration
    LEFT_COLS = ['Brand', 'Programme', 'Start Time', 'Duration (s)']
    n_left = len(LEFT_COLS)

    # Header row
    hdr_font = Font(bold=True, size=9)
    for ci, label in enumerate(LEFT_COLS, start=1):
        c = ws.cell(1, ci, label)
        c.font = hdr_font
    for di, d in enumerate(all_dates, start=n_left + 1):
        c = ws.cell(1, di, d.strftime('%d/%m'))
        c.font = hdr_font
        c.alignment = Alignment(horizontal='center')
    for ei, d_str in enumerate(sorted_extra_dates, start=n_left + len(all_dates) + 1):
        c = ws.cell(1, ei, d_str)
        c.font = hdr_font
        c.alignment = Alignment(horizontal='center')

    # Build date → output column mapping
    date_str_to_col = {d.strftime('%Y-%m-%d'): n_left + i for i, d in enumerate(all_dates, start=1)}
    for ei, d_str in enumerate(sorted_extra_dates, start=1):
        date_str_to_col[d_str] = n_left + len(all_dates) + ei

    # Data rows
    current_brand = None
    out_row = 2
    for (brand, programme, start_time, duration), date_counts in sorted(slot_groups.items()):
        if brand != current_brand:
            # Brand header row
            c = ws.cell(out_row, 1, brand)
            c.font = Font(bold=True, size=9)
            out_row += 1
            current_brand = brand

        # Write left columns
        ws.cell(out_row, 1, brand).font = Font(size=9)
        ws.cell(out_row, 2, programme).font = Font(size=9)
        ws.cell(out_row, 3, start_time).font = Font(size=9)
        ws.cell(out_row, 4, duration).font = Font(size=9)

        # Write date columns
        slot_key = (_normalize(programme), _normalize(start_time), duration)
        slot_status = (status_map or {}).get(slot_key, {})

        for d_str, planned_count in sorted(date_counts.items()):
            col = date_str_to_col.get(d_str)
            if not col:
                continue
            if not status_map:
                status = 'planned'
                count = planned_count
            else:
                d_data = slot_status.get(d_str, {})
                statuses = _ordered_statuses({k: v for k, v in d_data.items() if k != 'late_actual'})
                if statuses:
                    status, count = statuses[0]
                else:
                    status, count = 'planned', planned_count
            cell = ws.cell(out_row, col, count)
            cell.fill = status_to_fill.get(status, fills['planned'])
            cell.font = Font(bold=True, size=9)
            cell.alignment = Alignment(horizontal='center')

        # Extra late-telecast columns
        if status_map:
            for d_str, extra_col_idx in [(d, date_str_to_col[d]) for d in sorted_extra_dates]:
                count = sum(
                    d_dict.get('late_actual', {}).get(d_str, 0)
                    for d_dict in slot_status.values()
                )
                if count:
                    cell = ws.cell(out_row, extra_col_idx, count)
                    cell.fill = fills['late_telecast']
                    cell.font = Font(bold=True, size=9)
                    cell.alignment = Alignment(horizontal='center')

        out_row += 1

    # Legend
    legend_row = out_row + 1
    ws.cell(legend_row, 1, 'LEGEND:').font = Font(bold=True, size=9)
    legend_items = [
        ('Aired as Planned', fills['aired']),
        ('Not Aired',        fills['not_aired']),
        ('Late / Diff Date', fills['late_telecast']),
        ('Prog Mismatch',    fills['programme_mismatch']),
        ('Extra Aired',      fills['extra_aired']),
        ('Planned Only',     fills['planned']),
    ]
    for i, (label, fill) in enumerate(legend_items, start=2):
        c = ws.cell(legend_row, i, f'  {label}  ')
        c.fill = fill
        c.font = Font(bold=True, size=8)
        c.alignment = Alignment(horizontal='center')

    ws.append(['', '', '', '', 'NOTE: Rebuilt from DB — original file unavailable.'])
    return wb


# ── Workbook builder ──────────────────────────────────────────────────────────

def build_colored_schedule_wb(schedule_pk, colors: dict, status_map=None):
    """
    Generate a colored pivot-format Excel workbook.

    Parameters
    ----------
    schedule_pk : int
        Primary key of the Schedule object whose original file is the template.
    colors : dict
        {status_key: '#rrggbb'} — keys:
            'aired', 'not_aired', 'late_telecast', 'programme_mismatch',
            'extra_aired', 'planned'
    status_map : dict or None
        Output of build_status_map(). None → all date cells in 'planned' colour.

    Returns
    -------
    openpyxl.Workbook
    """
    import openpyxl
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill
    from openpyxl.utils import get_column_letter
    from core.models import Schedule

    schedule = Schedule.objects.get(pk=schedule_pk)

    # Load the original file bytes from storage (works for local + Firebase)
    raw_bytes = None
    try:
        raw_bytes = schedule.file.read()
    except Exception as exc:
        logger.warning('Cannot read original schedule file (%s): %s — falling back to DB reconstruction', schedule_pk, exc)

    if raw_bytes:
        wb_src = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True)
        ws_src = wb_src.active
        header_row_idx, col_map, date_col_map = _detect_pivot_openpyxl(ws_src)
        if not header_row_idx or not date_col_map:
            wb_dst = openpyxl.load_workbook(io.BytesIO(raw_bytes))
            ws_dst = wb_dst.active
            ws_dst.cell(1, 1, 'WARNING: Pivot structure not detected. Colours not applied.')
            return wb_dst
    else:
        # Original file missing (e.g. Railway redeploy wiped /media/) — reconstruct from DB
        return _build_wb_from_db(schedule, colors, status_map)

    # Build colour fills
    fills = {
        'aired':               _hex_to_fill(colors.get('aired',               '#22c55e')),
        'not_aired':           _hex_to_fill(colors.get('not_aired',           '#ef4444')),
        'late_telecast':       _hex_to_fill(colors.get('late_telecast',       '#a855f7')),
        'programme_mismatch':  _hex_to_fill(colors.get('programme_mismatch',  '#f97316')),
        'extra_aired':         _hex_to_fill(colors.get('extra_aired',         '#3b82f6')),
        'planned':             _hex_to_fill(colors.get('planned',             '#94a3b8')),
    }
    # matched/manual_match both use 'aired' fill
    status_to_fill = {
        'matched':             fills['aired'],
        'manual_match':        fills['aired'],
        'not_aired':           fills['not_aired'],
        'late_telecast':       fills['late_telecast'],
        'programme_mismatch':  fills['programme_mismatch'],
        'extra_aired':         fills['extra_aired'],
        'planned':             fills['planned'],
    }

    # Collect any EXTRA date columns needed for late-telecast actual dates
    extra_dates: set = set()
    if status_map:
        for slot_dict in status_map.values():
            for d_dict in slot_dict.values():
                for actual_date in (d_dict.get('late_actual') or {}).keys():
                    if actual_date not in date_col_map.values():
                        extra_dates.add(actual_date)
    sorted_extra_dates = sorted(extra_dates)

    # Assign column indices for extra dates (after last existing date column)
    max_existing_col  = max(date_col_map.keys()) if date_col_map else ws_src.max_column
    extra_date_col_map: dict = {}   # date_str → 1-based col index in output
    for i, d in enumerate(sorted_extra_dates, start=1):
        extra_date_col_map[d] = max_existing_col + i

    # Full date col map for output (existing + extra)
    all_date_cols: dict = dict(date_col_map)          # src_col → date_str
    # existing date cols keep same column index in output

    # ── Build a list of output rows (each is a list of (col, value, fill)) ──
    # We'll collect rows in a list first so we can append sub-rows as needed.
    output_rows = []   # list of list-of-dicts: [{col, value, fill, font, alignment}]
    src_row_meta = []  # parallel list: {'type': 'data'/'other', 'src_row': list_of_cells}

    # Track current brand context for slot key matching
    current_brand = ''

    for row_idx, src_row in enumerate(
        ws_src.iter_rows(min_row=1, max_row=ws_src.max_row), start=1
    ):
        row_cells = list(src_row)

        # Build base output cells (copy everything from source)
        out_cells = []
        for cell in row_cells:
            out_cells.append({
                'col':       cell.column,
                'value':     cell.value,
                'fill':      None,    # will be overwritten for date cols on data rows
                'src_cell':  cell,
            })
        # Also prepare empty cells for extra date columns
        for d, col in extra_date_col_map.items():
            out_cells.append({
                'col':      col,
                'value':    None,
                'fill':     None,
                'src_cell': None,
            })

        if row_idx < header_row_idx:
            # Pre-header rows: copy as-is
            output_rows.append(out_cells)
            src_row_meta.append({'type': _ROW_OTHER})
            continue

        if row_idx == header_row_idx:
            # Header row: add extra date column headers
            for d, col in extra_date_col_map.items():
                for oc in out_cells:
                    if oc['col'] == col:
                        oc['value'] = d   # date string as header
                        break
            output_rows.append(out_cells)
            src_row_meta.append({'type': _ROW_OTHER})
            continue

        # Data / brand / section rows
        prog_cell_val = str(row_cells[col_map['programme'] - 1].value or '').strip()

        if _is_skip_row(prog_cell_val):
            output_rows.append(out_cells)
            src_row_meta.append({'type': _ROW_OTHER})
            continue

        if not _is_data_row(row_cells, col_map):
            # Brand / section header row
            current_brand = prog_cell_val
            output_rows.append(out_cells)
            src_row_meta.append({'type': _ROW_OTHER})
            continue

        # ── Data row ──────────────────────────────────────────────────────────
        prog       = _normalize(prog_cell_val)
        start_raw  = row_cells[col_map['start_time'] - 1].value
        start_time = _normalize(str(start_raw or ''))
        try:
            dur = int(float(row_cells[col_map['duration'] - 1].value or 0))
        except (ValueError, TypeError):
            dur = 0

        slot_key = (prog, start_time, dur)

        # We may need to write sub-rows for mixed statuses.
        # For each date column, determine per-status counts.
        # Structure: {status: {date_col: count}}
        # Then order: main row gets 1st status, sub-rows get the rest.
        # All sub-rows share the same left-column values (blank).

        # Collect per-date status data
        per_date: dict = {}   # date_str → {status: count, 'late_actual': {...}}
        for src_col, date_str in all_date_cols.items():
            src_cell_val = row_cells[src_col - 1].value if src_col <= len(row_cells) else None
            try:
                planned_count = int(float(src_cell_val)) if src_cell_val is not None else 0
            except (ValueError, TypeError):
                planned_count = 0

            if not planned_count:
                per_date[date_str] = {}
                continue

            if status_map is None:
                per_date[date_str] = {'planned': planned_count}
            else:
                slot_data = status_map.get(slot_key, {})
                date_data = slot_data.get(date_str, {})
                if not date_data:
                    per_date[date_str] = {'planned': planned_count}
                else:
                    counts = {
                        k: v for k, v in date_data.items()
                        if k != 'late_actual' and v > 0
                    }
                    per_date[date_str] = counts
                    if 'late_actual' in date_data:
                        per_date[date_str]['late_actual'] = date_data['late_actual']

        # Also check extra date columns for late telecast arrivals
        for date_str, extra_col in extra_date_col_map.items():
            slot_data = status_map.get(slot_key, {}) if status_map else {}
            count = 0
            for orig_date, d_dict in slot_data.items():
                la = d_dict.get('late_actual', {})
                count += la.get(date_str, 0)
            if count:
                per_date[date_str] = per_date.get(date_str, {})
                per_date[date_str]['late_telecast'] = (
                    per_date[date_str].get('late_telecast', 0) + count
                )

        # Determine maximum number of sub-rows needed across all dates
        # (e.g. 3 statuses in one date → 2 sub-rows in addition to main)
        max_sub_rows = 0
        for date_str, counts in per_date.items():
            statuses = [s for s in counts if s not in ('late_actual',) and counts[s] > 0]
            max_sub_rows = max(max_sub_rows, len(statuses) - 1)

        # Build main row + sub-rows
        # Main row: first status per date
        # Sub-rows[i]: (i+1)-th status per date (blank left columns)

        sub_rows = [[] for _ in range(max_sub_rows)]

        # Update date cells in main out_cells
        for src_col, date_str in all_date_cols.items():
            counts = per_date.get(date_str, {})
            statuses = _ordered_statuses(counts)
            # Find the out_cells entry for this column
            main_entry = next((oc for oc in out_cells if oc['col'] == src_col), None)
            if not statuses:
                if main_entry:
                    main_entry['value'] = None
                continue
            # Main row
            if main_entry:
                s0, c0 = statuses[0]
                main_entry['value'] = c0
                main_entry['fill']  = status_to_fill.get(s0, fills['planned'])
            # Sub-rows
            for i, (si, ci) in enumerate(statuses[1:]):
                sub_rows[i].append({'col': src_col, 'status': si, 'count': ci})

        # Extra date columns (late telecast actual arrivals)
        for date_str, extra_col in extra_date_col_map.items():
            counts = per_date.get(date_str, {})
            statuses = _ordered_statuses(counts)
            main_entry = next((oc for oc in out_cells if oc['col'] == extra_col), None)
            if not statuses:
                if main_entry:
                    main_entry['value'] = None
                continue
            if main_entry:
                s0, c0 = statuses[0]
                main_entry['value'] = c0
                main_entry['fill']  = status_to_fill.get(s0, fills['planned'])
            for i, (si, ci) in enumerate(statuses[1:]):
                if i < len(sub_rows):
                    sub_rows[i].append({'col': extra_col, 'status': si, 'count': ci})

        output_rows.append(out_cells)
        src_row_meta.append({'type': _ROW_DATA, 'n_sub': max_sub_rows})

        # Append sub-rows (blank left columns, coloured date cells only)
        for sub_list in sub_rows:
            sub_out = [{
                'col': oc['col'], 'value': None, 'fill': None, 'src_cell': None
            } for oc in out_cells if oc['col'] not in {x['col'] for x in sub_list}]
            for entry in sub_list:
                sub_out.append({
                    'col':   entry['col'],
                    'value': entry['count'],
                    'fill':  status_to_fill.get(entry['status'], fills['planned']),
                    'src_cell': None,
                })
            output_rows.append(sub_out)
            src_row_meta.append({'type': _ROW_OTHER})

    # ── Write output workbook ─────────────────────────────────────────────────
    wb_dst = Workbook()
    ws_dst = wb_dst.active
    ws_dst.title = ws_src.title or 'Colored Schedule'

    # Copy column widths
    for col_letter, dim in ws_src.column_dimensions.items():
        ws_dst.column_dimensions[col_letter].width = dim.width
    for extra_col in extra_date_col_map.values():
        ws_dst.column_dimensions[get_column_letter(extra_col)].width = 10

    # Copy row heights where available
    src_rows_list = list(ws_src.iter_rows())
    for r_idx, out_cells in enumerate(output_rows, start=1):
        src_r = r_idx if r_idx <= len(src_rows_list) else None
        if src_r and src_r in ws_src.row_dimensions:
            ws_dst.row_dimensions[r_idx].height = ws_src.row_dimensions[src_r].height

        for oc in sorted(out_cells, key=lambda x: x['col']):
            dst_cell = ws_dst.cell(row=r_idx, column=oc['col'])
            dst_cell.value = oc['value']
            src_c = oc.get('src_cell')
            if src_c:
                _copy_cell_style(src_c, dst_cell)
            if oc.get('fill'):
                dst_cell.fill = oc['fill']
                dst_cell.font = Font(bold=True, size=9)
                dst_cell.alignment = Alignment(horizontal='center', vertical='center')

    # ── Legend ────────────────────────────────────────────────────────────────
    legend_row = len(output_rows) + 2
    legend_items = [
        ('Aired as Planned', fills['aired']),
        ('Not Aired',        fills['not_aired']),
        ('Late / Diff Date', fills['late_telecast']),
        ('Prog Mismatch',    fills['programme_mismatch']),
        ('Extra Aired',      fills['extra_aired']),
        ('Planned Only',     fills['planned']),
    ]
    ws_dst.cell(legend_row, 1, 'LEGEND:').font = Font(bold=True, size=9)
    for i, (label, fill) in enumerate(legend_items, start=2):
        c = ws_dst.cell(legend_row, i, f'  {label}  ')
        c.fill = fill
        c.font = Font(bold=True, size=8)
        c.alignment = Alignment(horizontal='center')

    return wb_dst


# ── Helpers ───────────────────────────────────────────────────────────────────

_STATUS_ORDER = [
    'matched', 'manual_match', 'not_aired', 'late_telecast',
    'programme_mismatch', 'extra_aired', 'planned',
]


def _ordered_statuses(counts: dict) -> list:
    """Return [(status, count)] in display order, skipping zeros and 'late_actual'."""
    result = []
    seen = set()
    for s in _STATUS_ORDER:
        if s in counts and counts[s] > 0:
            result.append((s, counts[s]))
            seen.add(s)
    for s, c in counts.items():
        if s not in seen and s != 'late_actual' and isinstance(c, int) and c > 0:
            result.append((s, c))
    return result
