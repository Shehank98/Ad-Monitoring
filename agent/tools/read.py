"""Read-only tools (T0). Every tool takes IDs and returns a JSON-safe dict/list."""
from __future__ import annotations

from core.models import Schedule
from verification.tc_engine import build_summary_data

from ..canonical import sha256_of, to_jsonable
from ..models import ScopeState, SummarySnapshot
from ..readiness import assess
from ..scope import active_schedules, makeup_schedules, period, standalone_keys, sync_scopes


def _scope_dict(sc: ScopeState) -> dict:
    return {'scope_id': sc.id, 'account_id': sc.account_id, 'channel': sc.channel,
            'month': sc.month, 'state': sc.state, 'reason': sc.reason}


def list_open_scopes(account_ids: list[int] | None = None) -> list[dict]:
    """Every scope that has a Schedule and is not AUTHORISED, plus standalone TC scopes.
    Creates missing ScopeState rows (agent table only)."""
    out = []
    for sc in sync_scopes(account_ids):
        r = assess(sc)
        if r.state != 'AUTHORISED':
            d = _scope_dict(sc)
            d.update(state=r.state, reason=r.reason)
            out.append(d)
    for acc, ch, mo in standalone_keys(account_ids):
        out.append({'scope_id': None, 'account_id': acc, 'channel': ch, 'month': mo,
                    'state': 'STANDALONE', 'reason': ''})
    return out


def get_scope(scope_id: int) -> dict:
    """Scope identity and stored agent state."""
    return _scope_dict(ScopeState.objects.get(pk=scope_id))


def get_active_schedules(scope_id: int) -> dict:
    """Active schedules (Rule 12, engine order) and makeup schedules as the engine includes them."""
    sc = ScopeState.objects.get(pk=scope_id)
    active = active_schedules(sc)
    fields = ('id', 'schedule_number', 'version', 'start_date', 'end_date', 'is_locked', 'is_superseded')
    as_rows = lambda qs: to_jsonable([{f: getattr(s, f) for f in fields} for s in qs])
    return {'scope_id': sc.id, 'active': as_rows(active), 'makeup': as_rows(makeup_schedules(sc, active))}


def scope_readiness(scope_id: int) -> dict:
    sc = ScopeState.objects.get(pk=scope_id)
    r = assess(sc)
    return to_jsonable({
        'scope_id': sc.id, 'state': r.state, 'reason': r.reason, 'reason_label': r.reason_label,
        'start': r.start, 'end': r.end, 'lmrb_until': r.lmrb_until,
        'schedules': [{'schedule_id': s.schedule.id, 'schedule_number': s.schedule.schedule_number,
                       'has_tc': s.has_tc, 'rows': s.rows, 'matched': s.matched, 'pending': s.pending,
                       'pending_after_lmrb': s.pending_after_lmrb, 'sub_status': s.sub_status}
                      for s in r.schedules],
        'unmapped': r.unmapped, 'warnings': r.warnings,
    })


def lmrb_coverage(scope_id: int) -> dict:
    sc = ScopeState.objects.get(pk=scope_id)
    r = assess(sc)
    covered = bool(r.lmrb_until and r.end and r.lmrb_until >= r.end)
    return to_jsonable({'scope_id': sc.id, 'start': r.start, 'end': r.end,
                        'lmrb_until': r.lmrb_until, 'covered': covered})


def find_unmapped_brands(scope_id: int) -> list[dict]:
    r = assess(ScopeState.objects.get(pk=scope_id))
    return [{'brand': b, 'duration': d} for b, d in r.unmapped]


def summary(scope_id: int, persist: bool = False, run=None) -> dict:
    """build_summary_data(..., schedule_id=sid) for every active schedule (A4).
    persist=True stores one SummarySnapshot per schedule (agent table, T0)."""
    sc = ScopeState.objects.get(pk=scope_id)
    out = {}
    for s in active_schedules(sc):
        data = to_jsonable(build_summary_data(sc.account_id, sc.channel, sc.month, schedule_id=s.id))
        sha = sha256_of(data)
        if persist:
            SummarySnapshot.objects.create(scope=sc, schedule=s, schedule_number=s.schedule_number,
                                           kind='draft', data=data, sha256=sha, run=run)
        out[str(s.id)] = {'schedule_number': s.schedule_number, 'sha256': sha, 'data': data}
    return out
