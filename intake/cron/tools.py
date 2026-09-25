"""
Read-only intake tools (owner C1/C5, Q7). Used by BOTH the rules (rules.py) and the
LLM loop (runner.py); the LLM only ever sees their JSON results.

  detect_tc(ctx)                     file type, channel guess, dates, counts, <=20 themes,
                                     PDF reader agreement
  find_schedules(ctx)                active candidates whose channel and period fit
  get_schedule(ctx, schedule_id)     facts about ONE candidate returned by find_schedules
  check_brand_overlap(ctx, sid)      total / candidate / foreign / overlap / passes

Nothing here writes. Attachment bytes go to a temporary file that is always deleted.
Channel strings from the file or from AllowedSender.channel_hint are only used to
SEARCH; they are never stored anywhere.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from core.models import Schedule, ScheduleRow, TransmissionReport, get_setting_list
from core.views import _find_col, _safe_date, _safe_int, _safe_str, _tc_channel_prompt
from verification.engine import _lmrb_channel_q, active_schedule_ids
from verification.tc_converters.gemini_ai import GeminiError, is_configured as gemini_configured
from verification.tc_converters.gemini_ai import parse_pdf as gemini_parse_pdf
from verification.tc_converters.dispatch import get_converter
from verification.tc_engine import _brands_for_tc_theme, _build_reverse_tc_theme_map

from agent.models import AgentAuthorisation, AgentConfig

from ..models import AllowedSender

MAX_SAMPLE_THEMES = 20        # C5


def _norm(s) -> str:
    return str(s).lower().strip() if s else ''


@dataclass
class ToolContext:
    attachment: object
    rows: list = field(default_factory=list)          # parsed TC rows (never sent to the LLM)
    detect: dict | None = None
    candidates: dict = field(default_factory=dict)    # schedule_id -> Schedule (find_schedules)
    returned_ids: set = field(default_factory=set)    # ids the LLM may use
    _reverse_maps: dict = field(default_factory=dict)

    @property
    def email(self):
        return self.attachment.email

    def senders(self):
        return AllowedSender.for_sender(self.email.sender)

    def reverse_map(self, account_id):
        if account_id not in self._reverse_maps:
            self._reverse_maps[account_id] = _build_reverse_tc_theme_map(account_id)
        return self._reverse_maps[account_id]


# ── reading the file ─────────────────────────────────────────────────────────

def _aliases():
    """The same column aliases core's _parse_tc_rows uses (core/views.py), incl. the
    admin-configured extra aliases. A parity test keeps the two in step."""
    return {
        'Channel': ['Channel', 'Station', 'CHANNEL', 'channel'],
        'Date': ['Date', 'Aired Date', 'Prg Date', 'aired_date', 'AiredDate', 'Prg_Date',
                 *get_setting_list('tc_extra_date_aliases')],
        'Programme': ['Programme', 'Program', 'Prg Name', 'PrgName', 'programme',
                      *get_setting_list('tc_extra_programme_aliases')],
        'TC_Theme': ['TC_Theme', 'TC Theme', 'Advt_Theme', 'Advt_theme', 'Theme', 'theme', 'Product',
                     'Description', 'Ad Name', 'AdName', 'Ad_Name', *get_setting_list('tc_extra_theme_aliases')],
        'Duration': ['Duration', 'Dur', 'Seconds', 'Ad Dur', 'Duration_Sec',
                     *get_setting_list('tc_extra_duration_aliases')],
        'Aired_Time': ['Aired_Time', 'Advt_Time', 'Advt_time', 'advt_Time', 'Time', 'Aired Time', 'Ad Start',
                       'AdTime', 'AiredTime', *get_setting_list('tc_extra_time_aliases')],
    }


def read_rows(df: pd.DataFrame) -> tuple[list[dict], list[str], int, str]:
    """(rows, missing_columns, skipped_rows, channel_guess) — read-only mirror of the
    row filter in core _parse_tc_rows: a row counts when theme, aired time and date exist."""
    cols = {std: _find_col(df, *alts) for std, alts in _aliases().items()}
    missing = [c for c in ('Date', 'TC_Theme', 'Aired_Time') if cols[c] is None]
    rows, skipped = [], 0
    for _, r in df.iterrows():
        theme = _safe_str(r.get(cols['TC_Theme'], '')) if cols['TC_Theme'] else ''
        aired = _safe_str(r.get(cols['Aired_Time'], '')) if cols['Aired_Time'] else ''
        date = _safe_date(r.get(cols['Date'])) if cols['Date'] else None
        if not (theme and aired and date):
            skipped += 1
            continue
        rows.append({'date': date, 'aired_time': aired, 'tc_theme': theme,
                     'duration': _safe_int(r.get(cols['Duration'])) if cols['Duration'] else None,
                     'programme': _safe_str(r.get(cols['Programme'], '')) if cols['Programme'] else ''})
    channel = ''
    if cols['Channel'] is not None:
        vals = df[cols['Channel']].dropna().astype(str).str.strip()
        vals = vals[vals != '']
        channel = vals.mode().iloc[0] if not vals.empty else ''
    return rows, missing, skipped, channel


def _key(r):
    return (r['date'], str(r['aired_time']).strip(), _norm(r['tc_theme']), r['duration'])


def with_tempfile(data: bytes, suffix: str, fn):
    """Write bytes to a temp file, call fn(path), and always delete the file."""
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            path = tmp.name
        return fn(path)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _channel_hint(ctx) -> str:
    return next((s.channel_hint for s in ctx.senders() if s.channel_hint), '')


def parse_attachment(att, channel_for_pdf: str = '') -> dict:
    """Parse the stored bytes. For PDFs: heuristic converter AND Gemini when configured,
    compared on (date, aired_time, tc_theme, duration) (Amendment A6)."""
    data = bytes(att.content or b'')
    ext = (att.ext or '').lower()
    out = {'file_type': ext, 'ai_available': None, 'readers_disagree': False, 'df': None}
    if ext in ('xlsx', 'xls'):
        out['df'] = with_tempfile(data, f'.{ext}', lambda p: pd.read_excel(p, header=0))
        return out

    def read_pdf(path):
        heur = get_converter(channel_for_pdf).parse_pdf(path)
        ai = None
        if gemini_configured():
            try:
                ai = gemini_parse_pdf(path, channel=channel_for_pdf,
                                      extra_instructions=_tc_channel_prompt(channel_for_pdf))
            except GeminiError:
                ai = None
        return heur, ai
    heur, ai = with_tempfile(data, '.pdf', read_pdf)
    out['ai_available'] = ai is not None and not ai.empty
    if out['ai_available']:
        h_rows, a_rows = read_rows(heur)[0], read_rows(ai)[0]
        out['readers_disagree'] = {_key(r) for r in h_rows} != {_key(r) for r in a_rows}
        out['df'] = ai                                   # tc_pdf_convert's order: Gemini first
    else:
        out['df'] = heur
    return out


# ── the four tools ───────────────────────────────────────────────────────────

def detect_tc(ctx: ToolContext) -> dict:
    if ctx.detect is not None:
        return ctx.detect
    att = ctx.attachment
    hint = _channel_hint(ctx)
    try:
        parsed = parse_attachment(att, channel_for_pdf=hint)
    except Exception as exc:        # noqa: BLE001 — an unreadable file is a finding, not a crash
        ctx.detect = {'ok': False, 'error': f'could not read file: {type(exc).__name__}',
                      'file_type': att.ext}
        return ctx.detect
    df = parsed['df'] if parsed['df'] is not None else pd.DataFrame()
    rows, missing, skipped, channel_guess = read_rows(df)
    ctx.rows = rows
    dates = sorted({r['date'] for r in rows})
    themes = sorted({r['tc_theme'] for r in rows}, key=str.lower)
    ctx.detect = {
        'ok': True, 'file_type': att.ext,
        'channel_guess': channel_guess or hint,
        'date_min': dates[0].isoformat() if dates else None,
        'date_max': dates[-1].isoformat() if dates else None,
        'months': sorted({d.strftime('%Y-%m') for d in dates}),
        'row_count': len(rows), 'missing_columns': missing, 'skipped_rows': skipped,
        'distinct_themes': len(themes), 'sample_themes': themes[:MAX_SAMPLE_THEMES],
        'pdf': ({'ai_available': parsed['ai_available'], 'readers_disagree': parsed['readers_disagree']}
                if att.ext == 'pdf' else None),
    }
    return ctx.detect


def _accounts(ctx):
    """AllowedSender accounts for this sender; any entry with no accounts = all accounts."""
    senders = ctx.senders()
    if not senders or any(not s.accounts.exists() for s in senders):
        return None
    return sorted({a for s in senders for a in s.accounts.values_list('id', flat=True)})


def _window_end(s: Schedule):
    cfg = AgentConfig.objects.filter(pk=1).first()           # read only; never creates the row
    grace = cfg.grace_days if cfg else AgentConfig._meta.get_field('grace_days').default
    return (s.end_date + timedelta(days=grace)) if s.end_date else None


def find_schedules(ctx: ToolContext) -> dict:
    d = detect_tc(ctx)
    if not d.get('ok') or not d.get('date_min'):
        return {'candidates': [], 'note': 'no TC dates to match'}
    dmin = _safe_date(d['date_min'])
    dmax = _safe_date(d['date_max'])
    qs = Schedule.objects.filter(is_superseded=False)
    accs = _accounts(ctx)
    if accs is not None:
        qs = qs.filter(account_id__in=accs)
    channel = d.get('channel_guess') or ''
    if channel:
        qs = qs.filter(_lmrb_channel_q(channel))         # search only; never stored
    out = []
    active_cache = {}
    for s in qs.select_related('account').order_by('account__name', 'channel', 'month', 'schedule_number'):
        k = (s.account_id, s.channel, s.month)
        if k not in active_cache:
            active_cache[k] = set(active_schedule_ids(*k))
        if s.id not in active_cache[k]:
            continue
        end = _window_end(s)
        if s.start_date and end and (dmax < s.start_date or dmin > end):
            continue
        ctx.candidates[s.id] = s
        ctx.returned_ids.add(s.id)
        out.append({'schedule_id': s.id, 'schedule_number': s.schedule_number, 'client': s.account.name,
                    'channel': s.channel, 'month': s.month})
    return {'candidates': out}


def get_schedule(ctx: ToolContext, schedule_id) -> dict:
    try:
        sid = int(schedule_id)
    except (TypeError, ValueError):
        return {'error': 'schedule_id must be an integer returned by find_schedules'}
    s = ctx.candidates.get(sid)
    if s is None:
        return {'error': 'unknown schedule_id: use only ids returned by find_schedules'}
    dups = [n for n in (Schedule.objects.filter(account_id=s.account_id, channel=s.channel, month=s.month,
                                                 is_superseded=False)
                        .order_by().values_list('schedule_number', flat=True))]
    end = _window_end(s)
    return {
        'schedule_id': s.id, 'schedule_number': s.schedule_number, 'version': s.version,
        'client': s.account.name, 'channel': s.channel, 'month': s.month,
        'start_date': s.start_date.isoformat() if s.start_date else None,
        'end_date': s.end_date.isoformat() if s.end_date else None,
        'window_end': end.isoformat() if end else None,
        'active': s.id in set(active_schedule_ids(s.account_id, s.channel, s.month)),
        'locked': bool(s.is_locked),
        'authorised': AgentAuthorisation.objects.filter(schedule=s).exists(),
        'duplicate_active_number': dups.count(s.schedule_number) > 1,
        'has_tc': TransmissionReport.objects.filter(schedule=s).exists(),
    }


def check_brand_overlap(ctx: ToolContext, schedule_id) -> dict:
    """Owner Q7, using the engine's own resolver (reconcile_tc):
         total     = parsed rows
         candidate = rows resolving to a brand in the candidate schedule's ScheduleRows
         foreign   = rows resolving only to brands of OTHER accounts
         overlap   = candidate / total
       passes = overlap >= AgentConfig.min_brand_overlap and foreign == 0."""
    detect_tc(ctx)
    try:
        s = ctx.candidates.get(int(schedule_id))
    except (TypeError, ValueError):
        s = None
    if s is None:
        return {'error': 'unknown schedule_id: use only ids returned by find_schedules'}
    brands = {_norm(b) for b in ScheduleRow.objects.filter(schedule=s).order_by()
              .values_list('brand', flat=True).distinct()}
    own = ctx.reverse_map(s.account_id)
    from core.models import BrandMapping
    others = sorted(set(BrandMapping.objects.exclude(account_id=s.account_id).exclude(tc_theme='')
                        .order_by().values_list('account_id', flat=True).distinct()))
    total = len(ctx.rows)
    candidate = foreign = 0
    for r in ctx.rows:
        mine = _brands_for_tc_theme(r['tc_theme'], r['duration'], own)
        if any(b in brands for b in mine):
            candidate += 1
        elif not mine and any(_brands_for_tc_theme(r['tc_theme'], r['duration'], ctx.reverse_map(a))
                              for a in others):
            foreign += 1
    cfg = AgentConfig.objects.filter(pk=1).first()
    threshold = cfg.min_brand_overlap if cfg else AgentConfig._meta.get_field('min_brand_overlap').default
    overlap = round(candidate / total, 4) if total else 0.0
    return {'schedule_id': s.id, 'total': total, 'candidate': candidate, 'foreign': foreign,
            'overlap': overlap, 'threshold': threshold,
            'passes': bool(total) and overlap >= threshold and foreign == 0}


# ── instruction-like text (rules layer) ──────────────────────────────────────

SUSPICIOUS = [re.compile(p, re.I) for p in (
    r'ignore\s+(all\s+|any\s+|your\s+|the\s+|previous\s+|prior\s+)*(instructions|rules|prompt)',
    r'disregard\s+(all\s+|your\s+|the\s+|previous\s+)*(instructions|rules)',
    r'system\s+prompt', r'you\s+are\s+now\b', r'\bassistant\s*:', r'\bsystem\s*:',
    r'upload\s+(this|it|the\s+file)?\s*(to|into|under)\b', r'use\s+schedule\b',
    r'submit_decision', r'</?\s*data\s*>', r'\bnew\s+instructions\b', r'do\s+not\s+review',
)]


def suspicious_text(*texts) -> list[str]:
    hits = []
    for t in texts:
        for p in SUSPICIOUS:
            m = p.search(str(t or ''))
            if m:
                hits.append(m.group(0)[:60])
    return hits
