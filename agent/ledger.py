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

# S4 + Phase 3.1 owner decisions. tc_not_linked comes from readiness, recorded as TC_NOT_LINKED.
ACTIONABLE = ('NO_TC_MAPPING', 'NO_BRAND_MAPPING', 'LMRB_THEME_NO_ROWS', 'TC_NO_ROWS', 'SPONSORSHIP_NOT_RUN',
              'CHANNEL_VARIANT', 'TC_NOT_LINKED', 'WILDCARD_TC_THEME_COMMERCIAL', 'MANUAL_LOCK_LOST',
              'DUPLICATE_ACTIVE_NUMBER', 'SCHEDULE_LOCKED', 'V5_UNEXPLAINED', 'RECONCILE_PENDING')
INFO_ONLY = ('MAKEUP_LINKED', 'SCHEDULE_NUMBER_WIDTH', 'SUPERSEDED_ROWS_PRESENT', 'TIME_BELT_UNATTRIBUTED',
             'LOCK_ORPHANED', 'LMRB_MULTI_FLAG')
UNCOVERED = ()                     # every diagnose code is now decided (Phase 3.1)
STATES = ('BASELINE',)             # a state, not a finding: never in the ledger, proposals, precision or eval
# Owner exit criterion 2: the mapping group (reported as a group and per code).
MAPPING_GROUP = ('NO_TC_MAPPING', 'LMRB_THEME_NO_ROWS', 'NO_BRAND_MAPPING')
UNMAPPED_BRAND = MAPPING_GROUP
# Not diagnoses: made by the cycle from V5 / shadow runs; never labelled or scored.
CYCLE_CODES = ('V5_UNEXPLAINED', 'RECONCILE_PENDING')
V5_CAUSES = ('fingerprint_gap', 'core_bug', 'outside_data_fix', 'accepted')
CLOSE_AFTER_ABSENT = 2
RESOLUTIONS = ('mapping_changed', 'manual_match', 'alias_changed', 'tc_reuploaded', 'self_cleared')
HUMAN_RESOLUTIONS = RESOLUTIONS[:4]
OWNER_ONLY = 'owner_only'     # an owner label the agent has not (yet) found: a miss (agent_label_scopes)


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
        if f.code in STATES:
            continue
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


def human_changes(full: dict, relevant: dict) -> list[str]:
    """Every kind of human change in this observation's scope-relevant diff (for the inferred
    'action coverage' measure, S2a)."""
    out = []
    if 'brand_mappings' in relevant or 'tc_lmrb_theme_maps' in relevant:
        out.append('mapping_changed')
    if (relevant.get('manual_matches') or {}).get('added'):
        out.append('manual_match')
    changed = ((full or {}).get('settings') or {}).get('changed') or {}
    if any(k in ALIAS_KEYS for k in changed):
        out.append('alias_changed')
    if 'transmission_reports' in relevant:
        out.append('tc_reuploaded')
    return out


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
    rows = {r.key: r for r in FindingLedger.objects.filter(scope=scope).exclude(code__in=CYCLE_CODES)}
    stats = {'opened': 0, 'reopened': 0, 'seen': 0, 'absent': 0, 'closed': 0, 'proposals': 0}
    full = fp_diff(fp_old, fp_new) if fp_old else {}
    relevant = relevant_diff(full, fp_old, fp_new, ctx) if full else {}
    # S2a inputs: human changes seen now, and whether an agent finding was open before them
    stats['human_changes'] = human_changes(full, relevant)
    stats['open_before'] = [r.code for r in rows.values() if r.open and r.resolution != OWNER_ONLY]

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
            owner_only = row.resolution == OWNER_ONLY
            reopened = not row.open and not owner_only
            row.text, row.evidence, row.last_seen, row.absent_count = g['text'], g['evidence'], now, 0
            row.actionable = actionable
            fields = ['text', 'evidence', 'last_seen', 'absent_count', 'actionable']
            if reopened:
                row.open, row.resolved_at, row.resolution, row.resolution_evidence = True, None, '', {}
                row.reopen_count += 1
                fields += ['open', 'resolved_at', 'resolution', 'resolution_evidence', 'reopen_count']
                stats['reopened'] += 1
            elif owner_only:       # the agent now finds what the owner labelled: no longer a miss
                row.open, row.resolution, row.first_seen = True, '', now
                row.label = 'correct' if row.label == 'owner' else row.label
                fields += ['open', 'resolution', 'first_seen', 'label']
                stats['opened'] += 1
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


# ── Phase 3.1 T1: unexplained V5 changes persist until an admin acknowledges them ──

def open_v5_unexplained(scope, schedule_id, prev_data: dict | None, new_data: dict, v5_detail: dict,
                        actor=None, now=None) -> FindingLedger:
    """One actionable V5_UNEXPLAINED row per schedule per unexplained change (keyed by the new
    sha256, so the same change is never opened twice). It never closes automatically."""
    from .effects import diff_summaries
    now = now or timezone.now()
    new_sha = v5_detail.get('new_sha', '')
    # Keyed on the baseline snapshot the change was measured against (guardian 3.1): the baseline
    # moves forward every observation, so each unexplained change gets its own row, even when the
    # numbers flip back to a value that was acknowledged before.
    key = ledger_key(scope.id, schedule_id, 'V5_UNEXPLAINED',
                     f'base:{v5_detail.get("previous_snapshot_id")}>{new_sha[:16]}', None)
    d = diff_summaries(prev_data or {}, new_data)
    evidence = {'schedule_id': schedule_id, 'previous_sha': v5_detail.get('previous_sha'), 'new_sha': new_sha,
                'previous_snapshot_id': v5_detail.get('previous_snapshot_id'),
                'by_brand': d['by_brand'], 'totals': d['totals'],
                'fingerprint_diff': v5_detail.get('relevant_diff') or {},
                'account_wide_diff': v5_detail.get('fingerprint_diff') or {}}
    row, made = FindingLedger.objects.get_or_create(key=key, defaults={
        'scope': scope, 'schedule_id': schedule_id, 'code': 'V5_UNEXPLAINED', 'brand': '',
        'text': 'The Summary numbers changed and nothing recorded explains it (no agent action, no '
                'relevant change in mappings, uploads or settings).',
        'evidence': evidence, 'actionable': True, 'open': True, 'first_seen': now, 'last_seen': now})
    if made:
        row.proposal = _proposal(scope, row, actor)
        row.save(update_fields=['proposal'])
    return row


def open_v5_count(scope) -> int:
    return FindingLedger.objects.filter(scope=scope, code='V5_UNEXPLAINED', open=True).count()


def acknowledge_v5(row: FindingLedger, cause: str, note: str, actor, now=None) -> dict:
    """Human gate write (agent.FindingLedger only). Caller wraps it in gate.perform."""
    if row.code != 'V5_UNEXPLAINED' or not row.open:
        raise ValueError('only an open V5_UNEXPLAINED row can be acknowledged')
    if cause not in V5_CAUSES:
        raise ValueError(f'root cause must be one of {", ".join(V5_CAUSES)}')
    if not (note or '').strip():
        raise ValueError('a note is required')
    now = now or timezone.now()
    row.open, row.resolved_at, row.resolution = False, now, cause
    row.resolution_evidence = {'acknowledged_by': getattr(actor, 'email', ''), 'note': note.strip()[:1000],
                               'root_cause': cause}
    row.save(update_fields=['open', 'resolved_at', 'resolution', 'resolution_evidence'])
    if row.proposal_id and row.proposal.status == 'open':
        p = row.proposal
        p.status, p.decided_at, p.decided_by = 'superseded', now, actor
        p.decision_note = f'acknowledged: {cause}'
        p.save(update_fields=['status', 'decided_at', 'decided_by', 'decision_note'])
    return {'open': False, 'resolution': cause, 'note': row.resolution_evidence['note']}


# ── Phase 3.1 T5: RECONCILE_PENDING (a reconcile would change the numbers) ─────

PENDING_MIN = 1          # |Δ| >= 1 on Aired or Missed for any brand
PENDING_TEXT = 'Running reconciliation would change these numbers.'


def reconcile_pending(scope, observed_snaps: dict, actor=None, now=None) -> dict:
    """Per schedule: open when the latest PendingEffect has |Δ| >= 1 on Aired or Missed; close with
    'reconciled_by_human' when a later observed snapshot equals that shadow (a person ran the core
    reconcile), recording the seconds from opening to that resolution. `observed_snaps` =
    {schedule_id: the latest observed SummarySnapshot}."""
    from .models import PendingEffect
    now = now or timezone.now()
    out = {'opened': 0, 'closed': 0}
    for sid, obs in observed_snaps.items():
        pe = (PendingEffect.objects.filter(scope=scope, schedule_id=sid).select_related('shadow')
              .order_by('-created_at', '-id').first())
        key = ledger_key(scope.id, sid, 'RECONCILE_PENDING', None, None)
        row = FindingLedger.objects.filter(key=key).select_related('proposal').first()
        ev = (row.evidence or {}) if row else {}
        if row and row.open:
            row.last_seen = now
            if obs.sha256 == ev.get('shadow_sha'):
                _close_pending(row, 'reconciled_by_human', now)
                out['closed'] += 1
                continue
            if pe and pe.id != ev.get('pending_effect_id'):
                if pe.max_abs >= PENDING_MIN:
                    row.evidence = _pending_evidence(pe, ev.get('opened_at'))
                else:
                    _close_pending(row, 'no_longer_pending', now)
                    out['closed'] += 1
                    continue
            row.save(update_fields=['last_seen', 'evidence'])
            continue
        if pe is None or pe.max_abs < PENDING_MIN or pe.shadow.sha256 == obs.sha256:
            continue
        if row and pe.id == ev.get('pending_effect_id'):
            continue                               # this effect was already resolved
        evidence = _pending_evidence(pe, now.isoformat())
        if row is None:
            row = FindingLedger.objects.create(
                key=key, scope=scope, schedule_id=sid, code='RECONCILE_PENDING', text=PENDING_TEXT,
                evidence=evidence, actionable=True, open=True, first_seen=now, last_seen=now)
        else:
            row.open, row.resolved_at, row.resolution, row.resolution_evidence = True, None, '', {}
            row.evidence, row.first_seen, row.last_seen, row.absent_count = evidence, now, now, 0
            row.save()
        row.proposal = _proposal(scope, row, actor)
        row.save(update_fields=['proposal'])
        out['opened'] += 1
    return out


def _pending_evidence(pe, opened_at) -> dict:
    return {'pending_effect_id': pe.id, 'shadow_snapshot_id': pe.shadow_id, 'shadow_sha': pe.shadow.sha256,
            'observed_snapshot_id': pe.observed_id, 'max_abs': pe.max_abs, 'by_brand': pe.by_brand,
            'totals': pe.totals, 'opened_at': opened_at}


def _close_pending(row, resolution, now):
    from datetime import datetime
    opened = datetime.fromisoformat((row.evidence or {}).get('opened_at') or row.first_seen.isoformat())
    row.open, row.resolved_at, row.resolution = False, now, resolution
    row.resolution_evidence = {'seconds_to_resolve': round((now - opened).total_seconds(), 1)}
    row.save(update_fields=['open', 'resolved_at', 'resolution', 'resolution_evidence', 'last_seen'])
    if row.proposal_id and row.proposal.status == 'open':
        p = row.proposal
        p.status, p.decided_at, p.decision_note = 'superseded', now, f'finding resolved: {resolution}'
        p.save(update_fields=['status', 'decided_at', 'decision_note'])
