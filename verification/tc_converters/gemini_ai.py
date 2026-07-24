"""
Gemini-powered TC PDF converter.

Sends the whole PDF to the Gemini API (vision) and asks for the aired-spot
table as strict JSON. Unlike the layout heuristics in generic.py /
sirasa_tv.py, this handles wrapped theme lines, per-channel layout quirks and
scanned tables without channel-specific rules.

Exposes the same contract as the other converters:

    parse_pdf(pdf_path: str, channel: str = '') -> pd.DataFrame
        columns: Date (datetime.date), Programme, Aired_Time, TC_Theme, Duration

Configuration (ad_monitor/settings.py, from environment):
    GEMINI_API_KEY   - required; empty disables this converter
    GEMINI_TC_MODEL  - default "gemini-2.5-flash"

Raises GeminiError with a human-readable message on any failure so callers
can fall back to the heuristic converter and surface the reason.
"""

import base64
import json
import logging
from datetime import datetime

import pandas as pd
import requests

logger = logging.getLogger(__name__)

API_URL_TMPL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
REQUEST_TIMEOUT = 110   # seconds; must stay under the gunicorn worker timeout (120)

PROMPT = """This document is a TV Transmission Certificate (TC) from a Sri Lankan
television channel{channel_hint}. It certifies which advertisement spots were aired.

Extract EVERY aired spot row from the table(s). For each spot return:
- date: the aired date as YYYY-MM-DD. Dates in the file are DAY-FIRST
  (DD/MM/YYYY, DD-MM-YY or DD-Mon-YY like "1-Apr-26").
- programme: the programme/show name during which the spot aired. Do NOT
  include time-belt labels (like "9PM"), dates, or times. If the file has no
  programme column, return "".
- aired_time: the ACTUAL telecast time of the spot as HH:MM:SS in 24-hour
  format. Convert 12-hour times ("9.30 PM") to 24-hour. This is never a
  duration - ignore values like 00:00:30.
- tc_theme: the full advertisement / commercial / caption name exactly as
  printed, rejoining any text that wraps onto a second line. Do NOT include
  the programme name, times, dates, serial numbers or cut numbers.
- duration: the spot duration in whole seconds as an integer ("00:00:15" or
  "0:00:15" means 15; "30 sec" means 30).

Rules:
- One JSON object per aired spot, in the order they appear.
- Skip page headers, column headers, footers, page numbers, invoice/VAT/value
  lines, totals and summary sections.
- Never invent or merge rows. If a field is unreadable, use "" (or 0 for
  duration).
{channel_instructions}"""

CHANNEL_INSTRUCTIONS_TMPL = """
Channel-specific instructions for this document (they take priority over the
generic guidance above when they conflict):
{text}
"""


class GeminiError(Exception):
    """Raised when the Gemini API call fails or returns unusable output."""


def _get_settings():
    """API key + model from Django settings, falling back to the environment."""
    try:
        from django.conf import settings
        return (getattr(settings, 'GEMINI_API_KEY', '') or '',
                getattr(settings, 'GEMINI_TC_MODEL', '') or 'gemini-2.5-flash')
    except Exception:
        import os
        return (os.environ.get('GEMINI_API_KEY', ''),
                os.environ.get('GEMINI_TC_MODEL', 'gemini-2.5-flash'))


def is_configured() -> bool:
    return bool(_get_settings()[0])


def _time_str(val) -> str:
    """Normalize a time string to HH:MM:SS (best effort)."""
    s = str(val or '').strip()
    if not s:
        return ''
    parts = s.split(':')
    if len(parts) == 2:
        parts.append('00')
    if len(parts) == 3:
        try:
            h, m, sec = (int(p) for p in parts)
            if 0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60:
                return f'{h:02d}:{m:02d}:{sec:02d}'
        except ValueError:
            pass
    return s


def parse_pdf(pdf_path: str, channel: str = '', extra_instructions: str = '') -> pd.DataFrame:
    api_key, model = _get_settings()
    if not api_key:
        raise GeminiError('GEMINI_API_KEY is not configured.')

    with open(pdf_path, 'rb') as f:
        pdf_b64 = base64.b64encode(f.read()).decode('ascii')

    channel_hint = f' (channel: {channel})' if channel else ''
    channel_instructions = (
        CHANNEL_INSTRUCTIONS_TMPL.format(text=extra_instructions.strip())
        if extra_instructions and extra_instructions.strip() else ''
    )
    generation_config = {
        'temperature': 0,
        'maxOutputTokens': 65536,
        'responseMimeType': 'application/json',
        'responseSchema': {
            'type': 'ARRAY',
            'items': {
                'type': 'OBJECT',
                'properties': {
                    'date':       {'type': 'STRING'},
                    'programme':  {'type': 'STRING'},
                    'aired_time': {'type': 'STRING'},
                    'tc_theme':   {'type': 'STRING'},
                    'duration':   {'type': 'INTEGER'},
                },
                'required': ['date', 'aired_time', 'tc_theme'],
            },
        },
    }
    # Extraction needs no reasoning budget; disabling it cuts latency and cost.
    # Only flash models accept a zero budget.
    if 'flash' in model:
        generation_config['thinkingConfig'] = {'thinkingBudget': 0}

    payload = {
        'contents': [{
            'parts': [
                {'inline_data': {'mime_type': 'application/pdf', 'data': pdf_b64}},
                {'text': PROMPT.format(channel_hint=channel_hint,
                                       channel_instructions=channel_instructions)},
            ],
        }],
        'generationConfig': generation_config,
    }

    try:
        resp = requests.post(
            API_URL_TMPL.format(model=model),
            headers={'x-goog-api-key': api_key, 'Content-Type': 'application/json'},
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        raise GeminiError('Gemini API timed out - the PDF may be too large.')
    except requests.exceptions.RequestException as e:
        raise GeminiError(f'Could not reach the Gemini API: {e}')

    if resp.status_code != 200:
        detail = ''
        try:
            detail = resp.json().get('error', {}).get('message', '')
        except Exception:
            pass
        raise GeminiError(f'Gemini API error {resp.status_code}: {detail or resp.text[:200]}')

    data = resp.json()
    candidates = data.get('candidates') or []
    if not candidates:
        block = (data.get('promptFeedback') or {}).get('blockReason', 'no candidates returned')
        raise GeminiError(f'Gemini returned no output ({block}).')

    cand = candidates[0]
    finish = cand.get('finishReason', '')
    parts = (cand.get('content') or {}).get('parts') or []
    text = ''.join(p.get('text', '') for p in parts)
    if not text:
        raise GeminiError(f'Gemini returned empty output (finishReason: {finish or "unknown"}).')
    if finish == 'MAX_TOKENS':
        raise GeminiError('Gemini output was truncated - the TC has too many rows for one call.')

    try:
        raw_rows = json.loads(text)
    except json.JSONDecodeError:
        raise GeminiError('Gemini returned invalid JSON.')
    if not isinstance(raw_rows, list):
        raise GeminiError('Gemini returned an unexpected JSON shape.')

    records = []
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        try:
            date_val = datetime.strptime(str(r.get('date', '')).strip(), '%Y-%m-%d').date()
        except ValueError:
            continue
        theme = str(r.get('tc_theme') or '').strip()
        aired = _time_str(r.get('aired_time'))
        if not (theme and aired):
            continue
        try:
            dur = int(r.get('duration') or 0) or None
        except (TypeError, ValueError):
            dur = None
        records.append({
            'Date':       date_val,
            'Programme':  str(r.get('programme') or '').strip(),
            'Aired_Time': aired,
            'TC_Theme':   theme,
            'Duration':   dur,
        })

    logger.info('Gemini TC parse: %d/%d usable rows (model=%s)', len(records), len(raw_rows), model)
    return pd.DataFrame(records, columns=['Date', 'Programme', 'Aired_Time', 'TC_Theme', 'Duration'])
