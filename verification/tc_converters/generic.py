"""
Generic TC PDF parser - heuristic, channel-agnostic.

Works by grouping PDF text items by Y position, identifying lines that contain
a date (DD/MM/YYYY or DD-MM-YYYY), extracting times and duration from each
such line, then splitting remaining text into Programme (left column) vs
TC_Theme (right column) using the largest horizontal gap between words.

This mirrors the client-side JavaScript PDF.js heuristic parser.

Returns a pandas DataFrame with columns:
    Date        - datetime.date
    Programme   - str
    Aired_Time  - "HH:MM:SS" str
    TC_Theme    - str
    Duration    - int (seconds)
"""

import re
from datetime import datetime

import pandas as pd

# ── Patterns ───────────────────────────────────────────────────────────────────

DATE_RE     = re.compile(r'\b(\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\b')
TIME_RE     = re.compile(r'\b(\d{2}:\d{2}:\d{2}(?::\d{2})?)\b')

SKIP_RE = re.compile(
    r'(VAT No|Invoice No|Invoice Details|Date\s*:|From\s*:|To\s*:|'
    r'Contractor Number|Contract No|Schedule No|Client\s*:|Agent\s*:|'
    r'Ref No|Client Code|Agent Code|Total Value|Certified By|Checked By|'
    r'Prepared By|POWER HOUSE|UNLESS OBJECTIONS|Page \d+ of \d+|'
    r'Prog\. Date|Caption.Copy Title|Duration .sec.|Channel\s*:|Sr\.No\.|'
    r'MTV Channel|Braybrooke|TELECAST CERTIFICATE|Advertiser\s*:|'
    r'Address\s*:|Campaign Period|Account Executive|Co.Agency|'
    r'Release Order Ref|Traffic Order No|Brand\s*:|TOTAL SPOTS|'
    r'AUTHORISED SIGNATORY|System Generated|Digitally Signed|'
    r'Day Time|Duration Lan Mat|Cut No|Product Details|Posi|Net Rate|'
    r'Remarks|Bill Number|Booking Number|Deal Number|Total Spot|'
    r'We certify|Net Rate 1|Agency Name|Printed Date|Printed Time|'
    r'Printed By|Client Name|Agent Name)',
    re.IGNORECASE
)

MONEY_RE   = re.compile(r'Rs\s*:|^\s*[\d,]+\.\d{2}\s*$')
DOTS_RE    = re.compile(r'\.{4,}')
DUR_VALUES = {'5', '10', '15', '20', '25', '30', '45', '60'}


def _should_skip(text: str) -> bool:
    return bool(SKIP_RE.search(text) or MONEY_RE.search(text) or DOTS_RE.search(text))


def _detect_date_fmt(date_strings) -> str:
    """
    Infer DD/MM/YYYY vs MM/DD/YYYY from a collection of raw date strings.

    Logic: if any second part > 12 it cannot be a month → format is MM/DD/YYYY.
           if any first part > 12 it cannot be a day-as-month → format is DD/MM/YYYY.
           Otherwise default to DD/MM/YYYY.
    """
    for ds in date_strings:
        parts = re.split(r'[-/.]', ds)
        if len(parts) != 3:
            continue
        try:
            p1, p2 = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if p2 > 12:
            return '%m/%d/%Y'   # second part can't be month → month is first
        if p1 > 12:
            return '%d/%m/%Y'   # first part can't be month → day is first
    return '%d/%m/%Y'           # all ambiguous – fall back to DD/MM/YYYY


def _parse_date(raw: str, fmt: str = '%d/%m/%Y'):
    parts = re.split(r'[-/.]', raw)
    if len(parts) != 3:
        return None
    # Expand 2-digit years before parsing
    if len(parts[2]) == 2:
        parts[2] = '20' + parts[2]
    normalized = '/'.join(parts)
    alt = '%m/%d/%Y' if fmt == '%d/%m/%Y' else '%d/%m/%Y'
    for f in (fmt, alt):
        try:
            return datetime.strptime(normalized, f).date()
        except ValueError:
            pass
    return None


def _parse_time(t: str) -> str:
    """Keep only HH:MM:SS (drop frame count if present)."""
    return ':'.join(t.split(':')[:3])


def parse_pdf(pdf_path: str) -> pd.DataFrame:
    """
    Parse any TC PDF using heuristic column detection.

    Parameters
    ----------
    pdf_path : str
        Absolute path to the PDF file.

    Returns
    -------
    pd.DataFrame with columns: Date, Programme, Aired_Time, TC_Theme, Duration
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError(
            'pdfplumber is required for PDF TC upload. '
            'Install it with: pip install pdfplumber'
        ) from exc

    # ── Step 1: extract all words with bounding boxes ──────────────────────────
    all_words = []  # list of {text, x0, x1, top}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=3, y_tolerance=3) or []
            for w in words:
                all_words.append({
                    'text': w['text'],
                    'x0':   w['x0'],
                    'x1':   w['x1'],
                    'top':  round(w['top'], 1),
                })

    if not all_words:
        return pd.DataFrame(columns=['Date', 'Programme', 'Aired_Time', 'TC_Theme', 'Duration'])

    # ── Step 2: group words into lines by Y position (±3pt tolerance) ──────────
    all_words.sort(key=lambda w: (w['top'], w['x0']))
    lines = []       # list of lists of word dicts
    cur_line = [all_words[0]]
    cur_y    = all_words[0]['top']

    for w in all_words[1:]:
        if abs(w['top'] - cur_y) <= 3:
            cur_line.append(w)
        else:
            lines.append(cur_line)
            cur_line = [w]
            cur_y    = w['top']
    lines.append(cur_line)

    # ── Step 3: identify "record starts" (lines containing a date) ─────────────
    records = []     # list of {main_line, all_words}

    for line in lines:
        text = ' '.join(w['text'] for w in line)
        if _should_skip(text):
            continue
        if DATE_RE.search(text):
            records.append({'main_line': line, 'all_words': list(line)})
        elif records and len(text.strip()) > 2:
            records[-1]['all_words'].extend(line)

    # ── Step 3b: detect date format (DD/MM/YYYY vs MM/DD/YYYY) from all dates ───
    all_date_strs = []
    for rec in records:
        m = DATE_RE.search(' '.join(w['text'] for w in rec['main_line']))
        if m:
            all_date_strs.append(m.group(1))
    date_fmt = _detect_date_fmt(all_date_strs)

    # ── Step 4: parse each record ───────────────────────────────────────────────
    parsed = []

    for rec in records:
        main_text = ' '.join(w['text'] for w in rec['main_line'])
        all_text  = ' '.join(w['text'] for w in rec['all_words'])

        date_m = DATE_RE.search(main_text)
        if not date_m:
            continue
        raw_date = date_m.group(1)
        date_val = _parse_date(raw_date, date_fmt)
        if date_val is None:
            continue

        all_times = [m.group(1) for m in TIME_RE.finditer(all_text)]
        if not all_times:
            continue

        # Duration comes from a time that starts with 00:00: (e.g. 00:00:30)
        dur_time = next((t for t in all_times if t.startswith('00:00:')), None)
        advt_t   = next((t for t in all_times if not t.startswith('00:00:')), all_times[0])
        aired_time = _parse_time(advt_t)

        # Derive duration
        duration = None
        if dur_time:
            try:
                duration = int(dur_time.split(':')[2])
            except (IndexError, ValueError):
                pass
        if duration is None:
            # Look for a known duration value before the first time in the text
            ti = TIME_RE.search(all_text)
            if ti:
                before = all_text[:ti.start()].strip()
                lw = re.search(r'\b(\d+)\b$', before)
                if lw and lw.group(1) in DUR_VALUES:
                    duration = int(lw.group(1))
            if duration is None:
                # Remove all time-like patterns (HH:MM:SS[:FF]) before searching
                # for a standalone duration number, to avoid matching the hour
                # part of a time string (e.g. "10" in "10:25:38:20").
                text_no_times = TIME_RE.sub(' ', all_text)
                m = re.search(r'\b(10|15|20|25|30|45|60)\b', text_no_times)
                if m:
                    duration = int(m.group(1))

        # ── Filter words that are the date / times / noise ────────────────────
        raw_date_plain = raw_date  # e.g. "12/01/2025"
        keep = []
        for w in rec['all_words']:
            c = w['text'].strip()
            if not c or len(c) <= 1:
                continue
            if raw_date_plain in c:
                continue
            if TIME_RE.search(c):
                continue
            if re.match(r'^(Mon|Tue|Wed|We|Thu|Fri|Sat|Sun|BS|PO|PD|OTHERS|Rs\.?|[STE])$', c, re.IGNORECASE):
                continue
            if re.match(r'^CB\s*\d*$', c, re.IGNORECASE):
                continue
            if re.match(r'^[\d,]+\.\d+$', c):
                continue
            if re.match(r'^\.+$', c):
                continue
            if duration and c == str(duration):
                continue
            if dur_time and c == dur_time:
                continue
            if re.match(r'^\d{1,4}$', c) and w['x0'] < 50:
                continue  # serial number
            if re.match(r'^(ITNCOMM|VTVCOMM|ITNOPCL)\w+$', c, re.IGNORECASE):
                continue
            if re.match(r'^\d{5,}$', c):
                continue  # cut codes
            keep.append(w)

        if not keep:
            continue

        # ── Split into Programme (left) and Theme (right) via largest gap ─────
        keep_sorted = sorted(keep, key=lambda w: w['x0'])
        max_gap, split_idx = 0, -1
        for i in range(len(keep_sorted) - 1):
            gap = keep_sorted[i+1]['x0'] - keep_sorted[i]['x1']
            if gap > max_gap:
                max_gap, split_idx = gap, i

        if max_gap > 4 and split_idx >= 0:
            threshold = keep_sorted[split_idx]['x1'] + max_gap / 2
        else:
            threshold = 9999

        prog_parts, theme_parts = [], []
        for w in keep:
            if w['x0'] >= threshold:
                theme_parts.append(w['text'])
            else:
                crossed = threshold != 9999 and w['x1'] > threshold + 5
                sm = re.match(r'^(.*?)\s+-\s*(.*)$', w['text'])
                if sm and (threshold == 9999 or crossed) and len(sm.group(1).strip()) > 2 and len(sm.group(2).strip()) > 2:
                    prog_parts.append(sm.group(1).strip())
                    theme_parts.append(sm.group(2).strip())
                else:
                    prog_parts.append(w['text'])

        programme = ' '.join(prog_parts).strip().rstrip('-').strip()
        theme     = ' '.join(theme_parts).strip().lstrip('-').strip()

        # Last-ditch: if everything ended up in one bucket, try splitting on " - "
        if programme and not theme:
            sm = re.match(r'^(.*?)\s+-\s+(.+)$', programme)
            if sm and len(sm.group(1).strip()) > 2:
                programme, theme = sm.group(1).strip(), sm.group(2).strip()
        elif not programme and theme:
            sm = re.match(r'^(.*?)\s+-\s+(.+)$', theme)
            if sm and len(sm.group(1).strip()) > 2:
                programme, theme = sm.group(1).strip(), sm.group(2).strip()

        if duration and theme.endswith(str(duration)):
            theme = theme[:-len(str(duration))].strip()

        if not (programme or theme):
            continue

        parsed.append({
            'Date':       date_val,
            'Programme':  programme,
            'Aired_Time': aired_time,
            'TC_Theme':   theme,
            'Duration':   duration,
        })

    if not parsed:
        return pd.DataFrame(columns=['Date', 'Programme', 'Aired_Time', 'TC_Theme', 'Duration'])

    return pd.DataFrame(parsed)
