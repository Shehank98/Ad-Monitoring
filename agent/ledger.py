"""Findings ledger (Phase 3 e, S3, S4). Agent tables only.

observe(scope, findings, fp_old, fp_new, ctx, actor):
    called once per observed snapshot of the scope.
    - key = sha256(scope|schedule-or-none|code|brand-or-none|duration-or-none); findings that
      share a key in one observation are merged into one row
    - a finding seen again is updated (absent_count reset); a closed one that comes back is
      reopened (reopen_count + 1)
    - an open finding absent from this observation gets absent_count + 1; it closes only when
      absent from 2 consecutive observed snapshots. The resolution is read from the
      fingerprint diff of the observation where it first went missing:
      mapping_changed | manual_match | alias_changed | tc_reuploaded | self_cleared
    - S4: actionable codes get one info-only AgentProposal(kind='finding', tier=T4,
      apply_payload={}); it is superseded when the finding closes
"""
from __future__ import annotations

import hashlib

from django.utils import timezone

from .fingerprint import ALIAS_KEYS, diff as fp_diff, relevant_diff
from .models import AgentProposal, FindingLedger

# S4. tc_not_linked comes from readiness (reason), recorded as TC_NOT_LINKED.
ACTIONABLE = ('NO_TC_MAPPING', 'CHANNEL_VARIANT', 'TC_NOT_LINKED', 'WILDCARD_TC_THEME_COMMERCIAL',
              'MANUAL_LOCK_LOST', 'DUPLICATE_ACTIVE_NUMBER', 'SCHEDULE_LOCKED')
INFO_ONLY = ('MAKEUP_LINKED', 'SCHEDULE_NUMBER_WIDTH', 'SUPERSEDED_ROWS_PRESENT', 'TIME_BELT_UNATTRIBUTED',
             'LOCK_ORPHANED')
# Codes S4 does not cover: info only, reported to the owner.
UNCOVERED = ('TC_NO_ROWS', 'LMRB_THEME_NO_ROWS', 'SPONSORSHIP_NOT_RUN', 'LMRB_MULTI_FLAG', 'BASELINE')
UNMAPPED_BRAND = ('NO_TC_MAPPING',)
CLOSE_AFTER_ABSENT = 2
RESOLUTIONS = ('mapping_changed', 'manual_match', 'alias_changed', 'tc_reuploaded', 'self_cleared')


def ledger_key(scope_id, schedule_id, code, brand, duration) -> str:
    raw = (f'{scope_id}|{schedule_id if schedule_id is not None else "none"}|{code}'
           f'|{brand if brand else "none"}|{duration if duration is not None else "none"}')
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _fields(f) -> dict:
    ev = f.evidence or {}
    sid = ev.get('schedule_id')
    dur = ev.get('duration')
    return {'code': f.code, 'brand': f.brand or '', 'schedule_id': int(sid) if sid is not None else None,
            'duration': int(dur) if dur is not None else None}


def group(scope, findings) -> dict:
    """{key: {code, brand, schedule_id, duration, text, evidence}} with same-key findings merged."""
    out = {}
    for f in findings:
        k = _fields(f)
        key = ledger_key(scope.id, k['schedule_id'], k['code'], k['brand'], k['duration'])
        if key in out:
            g = out[key]
            g['text'] = g['text'] if f.text in g['text'] else f'{g["text"]} | {f.text}'
            g['evidence'].setdefault('merged', []).append(f.evidence or {})
        else:
            out[key] = {**k, 'text': f.text, 'evidence': dict(f.evidence or {}),
                        'sub_code': f.sub_code}
    return out


def resolution_for(full: dict, relevant: dict) -> tuple[str, dict]:
    """Which human change (from the scope-relevant fingerprint diff) explains the disappearance.
    Checked in this order; nothing found = self_cleared."""
    for sec in ('brand_mappings', 'tc_lmrb_theme_maps'):
        if sec in relevant:
            return 'mapping_changed', {sec: relevant[sec]}
    if (relevant.get('manual_matches') or {}).get('added'):
        return 'manual_match', {'manual_matches': relevant['manual_matches']}
    changed = ((full or {}).get('settings') or {}).get('changed') or {}
    alias = {k: v for k, v in changed.items() if k in ALIAS_KEYS}
    if alias:
        return 'alias_changed', {'settings': alias}
    if 'transmission_reports' in relevant:
        return 'tc_reuploaded', {'transmission_reports': relevant['transmission_reports']}
    return 'self_cleared', {}


def _proposal(scope, row, actor):
    return AgentProposal.objects.create(
        kind='finding', tier=4, status='open', action_type=f'finding_{row.code.lower()}'[:40], scope=scope,
        schedule_id=row.schedule_id, target_model='', target_pk='', before={}, after={},
        reason=row.text, evidence={'ledger_key': row.key, 'code': row.code, 'brand': row.brand,
                                   'duration': row.duration, **{'finding': row.evidence}},
        apply_payload={}, actor=actor)


def observe(scope, findings, fp_old: dict | None, fp_new: dict, ctx: dict, actor=None, now=None) -> dict:
    now = now or timezone.now()
    current = group(scope, findings)
    rows = {r.key: r for r in FindingLedger.objects.filter(scope=scope)}
    stats = {'opened': 0, 'reopened': 0, 'seen': 0, 'absent': 0, 'closed': 0, 'proposals': 0}
    full = fp_diff(fp_old, fp_new) if fp_old else {}
    relevant = relevant_diff(full, fp_old, fp_new, ctx) if full else {}

    for key, g in current.items():
        row = rows.get(key)
        actionable = g['code'] in ACTIONABLE
        if row is None:
            row = FindingLedger.objects.create(
                key=key, scope=scope, schedule_id=g['schedule_id'], code=g['code'], brand=g['brand'][:200],
                duration=g['duration'], text=g['text'], evidence=g['evidence'], actionable=actionable,
                open=True, first_seen=now, last_seen=now)
            stats['opened'] += 1
        else:
            reopened = not row.open
            row.text, row.evidence, row.last_seen, row.absent_count = g['text'], g['evidence'], now, 0
            row.actionable = actionable
            fields = ['text', 'evidence', 'last_seen', 'absent_count', 'actionable']
            if reopened:
                row.open, row.resolved_at, row.resolution, row.resolution_evidence = True, None, '', {}
                row.reopen_count += 1
                fields += ['open', 'resolved_at', 'resolution', 'resolution_evidence', 'reopen_count']
                stats['reopened'] += 1
            else:
                stats['seen'] += 1
            row.save(update_fields=fields)
        if actionable and (row.proposal_id is None or row.proposal.status != 'open'):
            row.proposal = _proposal(scope, row, actor)
            row.save(update_fields=['proposal'])
            stats['proposals'] += 1

    for key, row in rows.items():
        if key in current or not row.open:
            continue
        row.absent_count += 1
        fields = ['absent_count']
        if row.absent_count == 1:
            # the observation where it first went missing: keep the diff that may explain it
            res, ev = resolution_for(full, relevant)
            row.resolution_evidence = {'pending': res, 'diff': ev}
            fields.append('resolution_evidence')
        stats['absent'] += 1
        if row.absent_count >= CLOSE_AFTER_ABSENT:
            pending = (row.resolution_evidence or {}).get('pending') or 'self_cleared'
            row.open, row.resolved_at, row.resolution = False, now, pending
            row.resolution_evidence = (row.resolution_evidence or {}).get('diff') or {}
            fields += ['open', 'resolved_at', 'resolution', 'resolution_evidence']
            stats['closed'] += 1
            if row.proposal_id and row.proposal.status == 'open':
                p = row.proposal
                p.status, p.decided_at = 'superseded', now
                p.decision_note = f'finding resolved: {row.resolution}'
                p.save(update_fields=['status', 'decided_at', 'decision_note'])
        row.save(update_fields=fields)
    return stats
