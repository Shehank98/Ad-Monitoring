"""
Sirasa TV TC PDF parser.

Extracts ad transmission rows from Sirasa TV's PDF Transmission Certificate format.

Returns a pandas DataFrame with standard TC columns:
    Date        - datetime.date object
    Programme   - programme name string
    Aired_Time  - "HH:MM:SS" string
    TC_Theme    - caption / copy title string
    Duration    - integer seconds

Usage:
    from verification.tc_converters.sirasa_tv import parse_pdf
    df = parse_pdf('/path/to/sirasa_tc.pdf')
"""
import re
from datetime import datetime

import pandas as pd

# ── Patterns ──────────────────────────────────────────────────────────────────

# Data line: D/M/YYYY or DD/MM/YYYY  <programme>  HH:MM:SS[:FF]  <caption>  <dur>
DATA_LINE = re.compile(
    r'^(\d{1,2}/\d{1,2}/\d{4})'          # group 1: date (1 or 2 digit day/month)
    r'\s+(.+?)'                           # group 2: programme name
    r'\s+(\d{2}:\d{2}:\d{2}(?::\d{2})?)'# group 3: timecode HH:MM:SS[:FF]
    r'\s+(.+?)'                           # group 4: caption / copy title (Advt_theme)
    r'\s+(\d+)\s*$'                       # group 5: duration in seconds
)

META_PATTERNS = {
    'channel': re.compile(r'Channel\s*:\s*(.+)', re.IGNORECASE),
    'brand':   re.compile(r'Brand\s*:\s*(.+)', re.IGNORECASE),
    'month':   re.compile(r'Month\s*:\s*(.+)', re.IGNORECASE),
    'year':    re.compile(r'Year\s*:\s*(.+)', re.IGNORECASE),
}

# Lines that look like data lines but are actually headers / footers to skip
SKIP_PATTERNS = {
    re.compile(r'date\s+programme', re.IGNORECASE),
    re.compile(r'\(sec\)', re.IGNORECASE),
    re.compile(r'total\s+spots', re.IGNORECASE),
    re.compile(r'total\s+duration', re.IGNORECASE),
    re.compile(r'grand\s+total', re.IGNORECASE),
    re.compile(r'transmission\s+certificate', re.IGNORECASE),
    re.compile(r'page\s+\d+', re.IGNORECASE),
    re.compile(r'caption\s*/\s*copy\s*title', re.IGNORECASE),
}


def _is_skip(line: str) -> bool:
    for pat in SKIP_PATTERNS:
        if pat.search(line):
            return True
    return False


def _parse_timecode(tc: str) -> str:
    """Convert HH:MM:SS:FF → HH:MM:SS (drop frame count)."""
    parts = tc.split(':')
    return ':'.join(parts[:3])


def _parse_date(raw: str):
    """Convert M/D/YYYY (Sirasa TV uses month-first format) → datetime.date, or None."""
    for fmt in ('%m/%d/%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def parse_pdf(pdf_path: str) -> pd.DataFrame:
    """
    Parse a Sirasa TV TC PDF and return a standard TC DataFrame.

    Parameters
    ----------
    pdf_path : str
        Absolute path to the PDF file.

    Returns
    -------
    pd.DataFrame with columns: Date, Programme, Aired_Time, TC_Theme, Duration
    Rows that cannot be parsed are silently skipped.
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise ImportError(
            'pdfplumber is required for PDF TC upload. '
            'Install it with: pip install pdfplumber'
        ) from exc

    records = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ''
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or _is_skip(line):
                    continue

                m = DATA_LINE.match(line)
                if not m:
                    continue

                date_str, programme, timecode, caption, dur_str = m.groups()

                date_val = _parse_date(date_str)
                if date_val is None:
                    continue

                records.append({
                    'Date':       date_val,
                    'Programme':  programme.strip(),
                    'Aired_Time': _parse_timecode(timecode),
                    'TC_Theme':   caption.strip(),
                    'Duration':   int(dur_str),
                })

    if not records:
        return pd.DataFrame(columns=['Date', 'Programme', 'Aired_Time', 'TC_Theme', 'Duration'])

    return pd.DataFrame(records)
