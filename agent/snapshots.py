"""Phase 3 snapshots.

observe(scope):  what the core Summary page shows NOW: build_summary_data(schedule_id=sid) per
                 active schedule, read inside a READ ONLY, always-rolled-back guard (no engine
                 run). Stored as kind='observed' with the scope fingerprint. The only V5 baseline.
shadow(scope):   what an agent run WOULD produce: dry_run(reconcile_scope) (rolled back), stored
                 as kind='shadow' plus a PendingEffect per schedule. Never a V5 baseline, never
                 authorisable.
"""
from __future__ import annotations

from verification.tc_engine import build_summary_data

from .canonical import sha256_of, to_jsonable
from .db import guard
from .effects import diff_summaries
from .fingerprint import fingerprint, fingerprint_sha
from .models import PendingEffect, SummarySnapshot
from .scope import active_schedules


def read_observed(scope) -> tuple[dict, dict, list]:
    """(summaries {sid: data}, fingerprint, active schedules) — read only, rolled back."""
    active = active_schedules(scope)
    with guard(read_only=True):
        data = {s.id: to_jsonable(build_summary_data(scope.account_id, scope.channel, scope.month,
                                                     schedule_id=s.id)) for s in active}
        fp = fingerprint(scope)
    return data, fp, active


def store_observed(scope, data: dict, fp: dict, active, run=None) -> dict:
    fp_sha = fingerprint_sha(fp)
    return {s.id: SummarySnapshot.objects.create(
        scope=scope, schedule=s, schedule_number=s.schedule_number, kind='observed', data=data[s.id],
        sha256=sha256_of(data[s.id]), fingerprint=fp, fingerprint_sha256=fp_sha, run=run) for s in active}


def shadow(scope, observed: dict) -> dict:
    """Run the rolled-back dry run and store shadow snapshots + pending effects.
    `observed` = {schedule_id: observed SummarySnapshot} from this cycle. Returns the dry-run result."""
    from .models import AgentRun
    from .tools.reconcile import dry_run
    res = dry_run(scope.id)
    if res.get('status') != 'ok' and res.get('status') != 'unexplained_change':
        return res
    run = AgentRun.objects.filter(scope=scope, kind='dry_run').order_by('-id').first()
    for sid, sched in (res.get('schedules') or {}).items():
        sid = int(sid)
        obs = observed.get(sid)
        if obs is None:
            continue
        after = sched['after']
        sh = SummarySnapshot.objects.create(
            scope=scope, schedule_id=sid, schedule_number=sched['schedule_number'], kind='shadow',
            data=after, sha256=sha256_of(after), run=run)
        d = diff_summaries(obs.data, after)
        PendingEffect.objects.create(scope=scope, schedule_id=sid, observed=obs, shadow=sh,
                                     by_brand=d['by_brand'], totals=d['totals'], max_abs=d['max_abs'])
    return res
