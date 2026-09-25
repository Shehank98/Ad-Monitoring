"""
Golden check (Amendment A4, A11.3, P3).

Baseline = build_summary_data(..., schedule_id=sid) for every active schedule of the
listed scopes — what the Summary page shows when that schedule is selected.

  snapshot            record the baseline to JSON (read-only, rolled back)
  verify idempotent   the Phase 1 exit gate: in a transaction that ALWAYS rolls back,
                      record the baseline, run the agent's engine steps, record again;
                      the two must match exactly (sha256 of canonical JSON)
  verify rebuild      report only: needs env AGENT_DISPOSABLE_DB=1. In a transaction that
                      ALWAYS rolls back, clear engine-set state using existing engine
                      functions only (manual locks untouched), run the agent's steps and
                      compare with the snapshot. Differences are stop-and-ask (A12).

Scopes are selected from input but their channel/month strings are always taken from the
stored Schedule record (guardian check 3). Every engine call runs under ScopeLock inside
transaction.atomic() (guardian check 15). Refuses SQLite outside tests.
"""
from __future__ import annotations

import json
import os

from django.conf import settings
from django.db import connection, transaction

from core.models import Account, PeriodSponsorship, Schedule, SponsorshipLmrbAssignment
from verification.engine import run_scope
from verification.period_sponsorship_engine import reset_period_sponsorship
from verification.sponsorship_engine import remove_assignments
from verification.tc_engine import reconcile_tc

from .canonical import sha256_of
from .locks import fallback_lock, take_xact_lock
from .models import ScopeState
from .scope import active_schedules, lock_key
from .tools.reconcile import engine_steps, schedule_summaries


class GoldenRefused(Exception):
    pass


class _Rollback(Exception):
    pass


def _check_db():
    if connection.vendor != 'postgresql' and not getattr(settings, 'AGENT_GOLDEN_ALLOW_SQLITE', False):
        raise GoldenRefused('The golden check runs on PostgreSQL only (A11.3).')


def resolve(entries: list[dict]) -> list[ScopeState]:
    """entries: [{"account": id-or-name, "channel": "...", "month": "..."}].
    Returns UNSAVED ScopeState objects whose strings come from the Schedule record."""
    out = []
    for e in entries:
        acc = e['account']
        account = (Account.objects.filter(pk=acc).first() if str(acc).isdigit()
                   else Account.objects.filter(name=acc).first())
        if account is None:
            raise GoldenRefused(f'Unknown account {acc!r}')
        s = Schedule.objects.filter(account=account, channel=e['channel'], month=e['month']).first()
        if s is None:
            raise GoldenRefused(f'No Schedule for {account.name} / {e["channel"]!r} / {e["month"]!r} '
                                '(channel and month must match the stored strings exactly)')
        out.append(ScopeState(account_id=account.id, channel=s.channel, month=s.month))
    return out


def _label(sc: ScopeState) -> str:
    return f'{sc.account_id} | {sc.channel} | {sc.month}'


def _row_key(section, row):
    if section == 'commercial':
        return f"{row['product']} · {row['dur']}s"
    return f"{row.get('programme', '')} / {row['product']} · {row['dur']}s"


def diff_summary(a: dict, b: dict) -> list[dict]:
    """Per-brand differences between two build_summary_data results."""
    out = []

    def rows(d):
        r = {('commercial', _row_key('commercial', x)): x for x in d.get('commercial', [])}
        for sec in d.get('sponsorship', []):
            for x in sec.get('rows', []):
                r[('sponsorship', _row_key('sponsorship', {**x, 'programme': sec.get('programme')}))] = x
        return r
    ra, rb = rows(a), rows(b)
    for k in sorted(set(ra) | set(rb)):
        x, y = ra.get(k), rb.get(k)
        if x != y:
            cols = sorted(set((x or {}).keys()) | set((y or {}).keys()))
            out.append({'section': k[0], 'brand': k[1],
                        'changes': {c: [(x or {}).get(c), (y or {}).get(c)] for c in cols
                                    if (x or {}).get(c) != (y or {}).get(c)}})
    return out


def _in_locked_rollback(sc: ScopeState, fn):
    """Run fn() under ScopeLock inside a transaction that always rolls back."""
    box = {}
    with fallback_lock(lock_key(sc.account_id, sc.channel, sc.month)):
        try:
            with transaction.atomic():
                if connection.vendor == 'postgresql':
                    take_xact_lock(lock_key(sc.account_id, sc.channel, sc.month))
                box['value'] = fn()
                raise _Rollback
        except _Rollback:
            pass
    return box['value']


def snapshot(entries) -> dict:
    _check_db()
    out = {'scopes': []}
    for sc in resolve(entries):
        def take():
            active = active_schedules(sc)
            data = schedule_summaries(sc, active)
            return [{'schedule_id': s.id, 'schedule_number': s.schedule_number,
                     'sha256': sha256_of(data[s.id]), 'data': data[s.id]} for s in active]
        out['scopes'].append({'account_id': sc.account_id, 'channel': sc.channel, 'month': sc.month,
                              'schedules': _in_locked_rollback(sc, take)})
    return out


def verify_idempotent(entries) -> dict:
    """Exit gate A11.3: baseline == result after the agent's steps, per schedule."""
    _check_db()
    report = {'mode': 'idempotent', 'ok': True, 'scopes': []}
    for sc in resolve(entries):
        def run():
            active = active_schedules(sc)
            before = schedule_summaries(sc, active)
            steps = engine_steps(sc, active)
            after = schedule_summaries(sc, active)
            return [{'schedule_id': s.id, 'schedule_number': s.schedule_number,
                     'before_sha256': sha256_of(before[s.id]), 'after_sha256': sha256_of(after[s.id]),
                     'match': sha256_of(before[s.id]) == sha256_of(after[s.id]),
                     'diff': diff_summary(before[s.id], after[s.id])} for s in active], steps
        rows, steps = _in_locked_rollback(sc, run)
        ok = all(r['match'] for r in rows)
        report['ok'] &= ok
        report['scopes'].append({'scope': _label(sc), 'ok': ok, 'schedules': rows,
                                 'engine': steps})
    return report


def _clear_engine_state(sc: ScopeState, active) -> None:
    """Rebuild mode only. Existing engine functions only; manual locks untouched:
    run_scope 'reset' keeps ManualMatch rows; reconcile_tc 'reset' skips TC rows pinned by
    ManualMatch; only match_type='auto' sponsorship assignments are removed;
    period-sponsorship matches are engine-made."""
    if os.environ.get('AGENT_DISPOSABLE_DB') != '1':      # defence in depth: never trust the caller
        raise GoldenRefused('engine state may only be cleared on a disposable database')
    if not connection.in_atomic_block:
        raise GoldenRefused('engine state may only be cleared inside a rolled-back transaction')
    acc, ch, mo = sc.account_id, sc.channel, sc.month
    run_scope(acc, ch, mo, mode='reset')
    for s in active:
        reconcile_tc(acc, ch, mo, mode='reset', schedule_id=s.id)
    auto_ids = list(SponsorshipLmrbAssignment.objects.filter(
        account_id=acc, schedule_row__schedule__in=active, match_type='auto').values_list('id', flat=True))
    remove_assignments(acc, ch, mo, auto_ids)
    for ps in PeriodSponsorship.objects.filter(account_id=acc, channel=ch, month=mo):
        reset_period_sponsorship(ps)


def verify_rebuild(entries, baseline: dict | None = None) -> dict:
    """Report only (Phase 1). Differences are stop-and-ask, never a CI failure."""
    _check_db()
    if os.environ.get('AGENT_DISPOSABLE_DB') != '1':
        raise GoldenRefused('verify --mode rebuild needs AGENT_DISPOSABLE_DB=1 (disposable database only).')
    base = {}
    for s in (baseline or {}).get('scopes', []):
        for row in s['schedules']:
            base[row['schedule_id']] = row['data']
    report = {'mode': 'rebuild', 'differences': 0, 'scopes': []}
    for sc in resolve(entries):
        def run():
            active = active_schedules(sc)
            live = schedule_summaries(sc, active)
            _clear_engine_state(sc, active)
            engine_steps(sc, active)
            after = schedule_summaries(sc, active)
            rows = []
            for s in active:
                ref = base.get(s.id, live[s.id])
                rows.append({'schedule_id': s.id, 'schedule_number': s.schedule_number,
                             'baseline_sha256': sha256_of(ref), 'rebuilt_sha256': sha256_of(after[s.id]),
                             'match': sha256_of(ref) == sha256_of(after[s.id]),
                             'diff': diff_summary(ref, after[s.id])})
            return rows
        rows = _in_locked_rollback(sc, run)
        report['differences'] += sum(0 if r['match'] else 1 for r in rows)
        report['scopes'].append({'scope': _label(sc), 'schedules': rows})
    return report


def render(report: dict) -> str:
    lines = [f"# Golden check — {report['mode']}", '']
    if report['mode'] == 'idempotent':
        lines.append(f"Result: {'MATCH' if report['ok'] else 'MISMATCH (stop and ask, A12)'}")
    else:
        lines.append(f"Differences: {report['differences']}"
                     + (' — STOP AND ASK (A12)' if report['differences'] else ''))
    lines.append('')
    for sc in report['scopes']:
        lines.append(f"## {sc['scope']}")
        lines.append('')
        lines.append('| Schedule | Baseline sha256 | Result sha256 | Match |')
        lines.append('|---|---|---|---|')
        for r in sc['schedules']:
            b = r.get('before_sha256') or r.get('baseline_sha256')
            a = r.get('after_sha256') or r.get('rebuilt_sha256')
            lines.append(f"| #{r['schedule_number']} | `{b[:16]}` | `{a[:16]}` | {'yes' if r['match'] else '**no**'} |")
        for r in sc['schedules']:
            for d in r['diff']:
                lines.append(f"- #{r['schedule_number']} {d['section']} **{d['brand']}**: "
                             + ', '.join(f"{k} {v[0]} → {v[1]}" for k, v in d['changes'].items()))
        lines.append('')
    return '\n'.join(lines) + '\n'


def load_entries(path: str) -> list[dict]:
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    return data['scopes'] if isinstance(data, dict) else data
