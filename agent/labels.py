"""Owner labels (Phase 3 S2c, owner Q4).

CSV columns: account, channel, month, schedule_number, cause_code, brand, duration, as_of, note
  account          Account name (exact) or id
  channel, month   must match a Schedule EXACTLY (no case or whitespace changes, R2)
  schedule_number  an active (non-superseded) schedule of that scope
  cause_code       a diagnose code, TC_NOT_LINKED, no_issue or other
  brand, duration  optional; narrow the match
  as_of            YYYY-MM-DD, the day the label describes
  complete         optional, yes / no (default no): yes = these rows list every real cause for the scope

apply_labels(rows, actor): one human gate write (agent.FindingLedger) per row
  - a code: every ledger row of that scope with the code (and brand/duration when given) seen
    on or before as_of gets label='correct', label_source='owner'. None found = a miss: an
    owner-only ledger row (resolution 'owner_only', closed) records it.
  - no_issue: the scope's agent findings seen on or before as_of get label='incorrect'.
  - other: recorded as an owner-only row (a cause the agent has no code for).
"""
from __future__ import annotations

import csv
import datetime
from dataclasses import dataclass

from django.utils import timezone

from core.models import Account, Schedule

from . import gate
from .ledger import ACTIONABLE, CYCLE_CODES, INFO_ONLY, OWNER_ONLY, ledger_key
from .models import FindingLedger, ScopeState

COLUMNS = ('account', 'channel', 'month', 'schedule_number', 'cause_code', 'brand', 'duration', 'as_of', 'note')
# Phase 3.1 T4: optional column. complete=yes on any row of a scope means the owner listed EVERY real
# cause for that scope, so an agent finding there that no label explains is a false positive.
OPTIONAL_COLUMNS = ('complete',)
# diagnose codes only (BASELINE is a state; V5_UNEXPLAINED / RECONCILE_PENDING are not diagnoses)
CAUSE_CODES = tuple(sorted(set(ACTIONABLE) - set(CYCLE_CODES) | set(INFO_ONLY))) + ('no_issue', 'other')


class LabelError(ValueError):
    pass


@dataclass
class Label:
    line: int
    account: Account
    schedule: Schedule
    cause_code: str
    brand: str
    duration: int | None
    as_of: datetime.date
    note: str
    complete: bool = False


def parse(path: str) -> tuple[list[Label], list[str]]:
    """(labels, errors). A row with any problem is refused with its line number."""
    out, errors = [], []
    with open(path, newline='', encoding='utf-8-sig') as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            return [], [f'missing columns: {", ".join(missing)}']
        for n, r in enumerate(reader, start=2):
            try:
                out.append(_row(n, r))
            except LabelError as exc:
                errors.append(f'line {n}: {exc}')
    return out, errors


def _row(n, r) -> Label:
    a = (r['account'] or '').strip()
    acc = Account.objects.filter(name=a).first() or (Account.objects.filter(pk=int(a)).first() if a.isdigit() else None)
    if acc is None:
        raise LabelError(f'unknown account {a!r}')
    # channel / month exactly as stored: no strip, no case change
    qs = Schedule.objects.filter(account=acc, channel=r['channel'], month=r['month'],
                                 schedule_number=(r['schedule_number'] or '').strip(), is_superseded=False)
    s = qs.order_by('-version').first()
    if s is None:
        raise LabelError(f'no active schedule {r["schedule_number"]!r} for {acc.name} | {r["channel"]!r} | '
                         f'{r["month"]!r} (strings must match exactly)')
    code = (r['cause_code'] or '').strip()
    if code not in CAUSE_CODES:
        raise LabelError(f'unknown cause_code {code!r}')
    dur = (r['duration'] or '').strip()
    try:
        as_of = datetime.date.fromisoformat((r['as_of'] or '').strip())
    except ValueError as exc:
        raise LabelError(f'as_of must be YYYY-MM-DD: {exc}') from None
    if dur and not dur.isdigit():
        raise LabelError(f'duration must be whole seconds, not {dur!r}')
    comp = (r.get('complete') or 'no').strip().lower()
    if comp not in ('yes', 'no'):
        raise LabelError(f'complete must be yes or no, not {comp!r}')
    return Label(n, acc, s, code, (r['brand'] or '').strip(), int(dur) if dur else None, as_of,
                 (r['note'] or '').strip()[:500], comp == 'yes')


def _end_of(day):
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time()), tz)


def _matches(scope, lab: Label):
    qs = FindingLedger.objects.filter(scope=scope, first_seen__lt=_end_of(lab.as_of)).exclude(resolution=OWNER_ONLY)
    if lab.cause_code not in ('no_issue', 'other'):
        qs = qs.filter(code=lab.cause_code)
        if lab.brand:
            qs = qs.filter(brand__iexact=lab.brand)
        if lab.duration is not None:
            qs = qs.filter(duration=lab.duration)
    return qs


def apply_label(lab: Label, actor) -> dict:
    s = lab.schedule
    scope, _ = ScopeState.objects.get_or_create(account_id=s.account_id, channel=s.channel, month=s.month)
    rows = list(_matches(scope, lab)) if lab.cause_code != 'other' else []
    before = [{'id': r.id, 'label': r.label, 'label_source': r.label_source} for r in rows]

    def apply():
        changed, created = [], None
        common = {'label_source': 'owner', 'cause_code': lab.cause_code, 'label_note': lab.note,
                  'label_as_of': lab.as_of}
        if lab.cause_code == 'no_issue':
            for r in rows:
                FindingLedger.objects.filter(pk=r.pk).update(label='incorrect', **common)
                changed.append(r.id)
        elif rows:
            for r in rows:
                FindingLedger.objects.filter(pk=r.pk).update(label='correct', **common)
                changed.append(r.id)
        elif lab.cause_code != 'no_issue':      # a miss (or 'other'): the owner's cause on its own row
            now = timezone.now()
            key = ledger_key(scope.id, None, lab.cause_code, lab.brand, lab.duration)
            row, made = FindingLedger.objects.get_or_create(key=key, defaults={
                'scope': scope, 'code': lab.cause_code, 'brand': lab.brand[:200], 'duration': lab.duration,
                'text': f'Owner label: {lab.note or lab.cause_code}', 'actionable': lab.cause_code in ACTIONABLE,
                'open': False, 'first_seen': now, 'last_seen': now, 'resolution': OWNER_ONLY,
                'label': 'owner', **common})
            if not made:
                FindingLedger.objects.filter(pk=row.pk).update(**common)
            created = row.id
        return {'labelled': changed, 'owner_only_row': created}
    act = gate.perform(tier=gate.T0, action_type='owner_label', actor_kind='human', actor=actor, scope=scope,
                       target_model='agent.FindingLedger', target_pk=f'{scope.id}:{lab.cause_code}',
                       before={'rows': before}, apply=apply,
                       reason=f'owner label line {lab.line}: {lab.cause_code}',
                       evidence={'schedule_id': s.id, 'schedule_number': s.schedule_number,
                                 'brand': lab.brand, 'duration': lab.duration, 'as_of': lab.as_of.isoformat()})
    return act.after


def scope_key(lab: Label) -> tuple:
    s = lab.schedule
    return (s.account_id, s.channel, s.month)


def complete_scopes(labels) -> set:
    return {scope_key(l) for l in labels if l.complete}


def mark_unexplained_incorrect(labels, actor) -> int:
    """T4 for the ledger: in a complete=yes scope, every agent finding (diagnoses only) seen on or
    before as_of that no owner label explains is labelled incorrect. One human gate write per scope."""
    n = 0
    for key in complete_scopes(labels):
        labs = [l for l in labels if scope_key(l) == key]
        as_of = max(l.as_of for l in labs)
        scope = ScopeState.objects.filter(account_id=key[0], channel=key[1], month=key[2]).first()
        if scope is None:
            continue
        rows = list(FindingLedger.objects.filter(scope=scope, first_seen__lt=_end_of(as_of), label='')
                    .exclude(resolution=OWNER_ONLY).exclude(code__in=CYCLE_CODES))
        if not rows:
            continue

        def apply(rows=rows, labs=labs):
            ids = [r.id for r in rows]
            FindingLedger.objects.filter(pk__in=ids).update(
                label='incorrect', label_source='owner', cause_code='complete_unexplained',
                label_note='not in a complete owner label set', label_as_of=max(l.as_of for l in labs))
            return {'labelled_incorrect': ids}
        gate.perform(tier=gate.T0, action_type='owner_label_complete', actor_kind='human', actor=actor,
                     scope=scope, target_model='agent.FindingLedger', target_pk=f'{scope.id}:complete',
                     before={'rows': [{'id': r.id, 'label': r.label} for r in rows]}, apply=apply,
                     reason='complete=yes: unexplained agent findings are false positives')
        n += len(rows)
    return n
