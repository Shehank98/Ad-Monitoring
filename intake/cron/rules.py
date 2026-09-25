"""
Rules verdict R (owner C2): the code decides; the LLM can only downgrade.

evaluate(ctx) follows the prompt's steps 1-5 with the same tools the LLM uses, and
returns {'decision': propose|needs_review|ignore, 'schedule_id', 'reason', 'evidence'}.
combine(R, L, ...) applies the C2 table.
"""
from __future__ import annotations

import re

from .tools import check_brand_overlap, detect_tc, find_schedules, get_schedule, suspicious_text

# Order of reasons when a single candidate fails (prompt step 4)
FAIL_ORDER = ('schedule_frozen', 'schedule_locked', 'duplicate_active_number',
              'date_out_of_range', 'foreign_brands', 'low_brand_overlap')


def _verdict(decision, reason='', schedule_id=None, **evidence):
    return {'decision': decision, 'reason': reason or '', 'schedule_id': schedule_id, 'evidence': evidence}


def referenced_numbers(ctx, numbers: set[str]) -> set[str]:
    """Candidate schedule numbers that appear as whole tokens in subject/body/file name/themes."""
    text = ' '.join([ctx.email.subject or '', ctx.email.body_text or '', ctx.attachment.filename or '',
                     *((ctx.detect or {}).get('sample_themes') or [])])
    tokens = set(re.findall(r'[A-Za-z0-9][A-Za-z0-9/_.-]*', text))
    return {n for n in numbers if n in tokens}


def candidate_failures(info: dict, overlap: dict, d: dict) -> list[str]:
    fails = []
    if info.get('authorised'):
        fails.append('schedule_frozen')
    if info.get('locked'):
        fails.append('schedule_locked')
    if info.get('duplicate_active_number'):
        fails.append('duplicate_active_number')
    if not info.get('active'):
        fails.append('no_schedule')
    start, end = info.get('start_date'), info.get('window_end')
    if d.get('date_min') and start and end and (d['date_min'] < start or d['date_max'] > end):
        fails.append('date_out_of_range')
    if overlap.get('foreign'):
        fails.append('foreign_brands')
    elif not overlap.get('passes'):
        fails.append('low_brand_overlap')
    return fails


def evaluate(ctx) -> dict:
    att, em = ctx.attachment, ctx.email
    hits = suspicious_text(em.subject, em.body_text, att.filename)
    d = detect_tc(ctx)
    hits += suspicious_text(*(d.get('sample_themes') or []))
    if not d.get('ok'):
        return _verdict('needs_review', 'columns_unrecognised', detect=d, suspicious=hits)
    if d['missing_columns'] or d['skipped_rows'] or not d['row_count']:
        return _verdict('needs_review', 'columns_unrecognised', detect=d, suspicious=hits)
    if d.get('pdf') and d['pdf'].get('readers_disagree'):
        return _verdict('needs_review', 'pdf_disagreement', detect=d, suspicious=hits)
    if len(d.get('months') or []) > 1:
        return _verdict('needs_review', 'shared_tc', detect=d, suspicious=hits)

    cands = find_schedules(ctx)['candidates']
    if not cands:
        return _verdict('needs_review', 'no_schedule', detect=d, suspicious=hits, candidates=[])
    per = {}
    for c in cands:
        info = get_schedule(ctx, c['schedule_id'])
        ov = check_brand_overlap(ctx, c['schedule_id'])
        per[c['schedule_id']] = {'schedule': info, 'overlap': ov, 'fails': candidate_failures(info, ov, d)}
    eligible = [sid for sid, v in per.items() if not v['fails']]
    refs = referenced_numbers(ctx, {v['schedule']['schedule_number'] for v in per.values()})
    ev = {'detect': d, 'suspicious': hits, 'candidates': per, 'referenced_numbers': sorted(refs)}

    if len(eligible) == 1:
        sid = eligible[0]
        if refs and refs != {per[sid]['schedule']['schedule_number']}:
            return _verdict('needs_review', 'conflict', sid, **ev)
        return _verdict('propose', '', sid, **ev)
    if len(eligible) > 1 or len(per) > 1:
        return _verdict('needs_review', 'multiple_schedules', None, **ev)
    only = next(iter(per.values()))
    reason = next((r for r in FAIL_ORDER if r in only['fails']), only['fails'][0])
    return _verdict('needs_review', reason, only['schedule']['schedule_id'], **ev)


def combine(R: dict, L: dict | None, *, rules_only: bool, pdf_single_reader: bool,
            llm_error: str = '', rules_only_why: str = '') -> dict:
    """Owner C2. Returns {'status', 'reason', 'schedule_id', 'hint_schedule_id', 'why'}."""
    l_sus = bool(L and L.get('suspicious_instruction'))
    if l_sus or (R.get('evidence') or {}).get('suspicious'):
        return {'status': 'needs_review', 'reason': 'suspicious_instruction', 'schedule_id': None,
                'hint_schedule_id': None, 'why': 'instruction-like text in the email or file'}
    if R['decision'] == 'ignore':
        return {'status': 'ignored', 'reason': R['reason'], 'schedule_id': None, 'hint_schedule_id': None,
                'why': 'rules: not a TC'}
    hint = (L or {}).get('schedule_id')
    if R['decision'] == 'needs_review':
        return {'status': 'needs_review', 'reason': R['reason'], 'schedule_id': R.get('schedule_id'),
                'hint_schedule_id': hint, 'why': 'rules need a person'}
    # R = propose
    if rules_only or L is None:
        reason = 'tool_error' if llm_error else ''
        return {'status': 'needs_review', 'reason': reason, 'schedule_id': R['schedule_id'],
                'hint_schedule_id': None,
                'why': f'rules only ({llm_error or rules_only_why or "no model"}): a person reviews it'}
    if L.get('decision') != 'propose' or L.get('schedule_id') != R['schedule_id']:
        return {'status': 'needs_review', 'reason': 'llm_disagrees', 'schedule_id': R['schedule_id'],
                'hint_schedule_id': hint, 'why': 'the assistant and the rules disagree'}
    if pdf_single_reader:
        return {'status': 'needs_review', 'reason': '', 'schedule_id': R['schedule_id'],
                'hint_schedule_id': None, 'why': 'PDF read by one method only (Gemini not configured)'}
    return {'status': 'suggested', 'reason': '', 'schedule_id': R['schedule_id'], 'hint_schedule_id': None,
            'why': 'rules and assistant agree'}
