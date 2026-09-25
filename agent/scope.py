"""
Scope keys and active schedules (AGENT_BUILD_BRIEF.md §5, Amendment A4).

Active schedules come ONLY from the engine's own helpers (Rule 12 / Rule 10):
verification.engine.active_schedule_ids and _makeup_schedules_for_scope. Channel and
month strings are always read from Schedule records, never built or re-cased (R2).
"""
from __future__ import annotations

import hashlib

from django.db.models import Max, Min

from core.models import Schedule, ScheduleRow, TransmissionReport
from verification.engine import _makeup_schedules_for_scope, active_schedule_ids

from .models import ScopeState

COMMERCIAL = 'COMMERCIAL BENEFITS'


def lock_key(account_id, channel: str, month: str) -> int:
    """Signed 64-bit key from sha256(account_id|channel|month) (Amendment P2)."""
    digest = hashlib.sha256(f'{account_id}|{channel}|{month}'.encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big', signed=True)


def sync_scopes(account_ids=None) -> list[ScopeState]:
    """Ensure a ScopeState exists for every (account, channel, month) that has a
    Schedule. Strings are copied from the Schedule rows exactly. Agent tables only."""
    qs = Schedule.objects.all()
    if account_ids is not None:
        qs = qs.filter(account_id__in=account_ids)
    out = []
    for acc, channel, month in qs.order_by().values_list('account_id', 'channel', 'month').distinct():
        st, _ = ScopeState.objects.get_or_create(account_id=acc, channel=channel, month=month)
        out.append(st)
    return out


def standalone_keys(account_ids=None):
    """(account, channel, month) of TransmissionReports whose scope has no Schedule at all."""
    qs = TransmissionReport.objects.filter(schedule__isnull=True)
    if account_ids is not None:
        qs = qs.filter(account_id__in=account_ids)
    keys = set(qs.order_by().values_list('account_id', 'channel', 'month').distinct())
    return sorted(k for k in keys
                  if not Schedule.objects.filter(account_id=k[0], channel=k[1], month=k[2]).exists())


def active_schedules(scope: ScopeState) -> list[Schedule]:
    """Primary active schedules in engine order (schedule_number, Rule 10)."""
    ids = active_schedule_ids(scope.account_id, scope.channel, scope.month)
    by_id = Schedule.objects.in_bulk(ids)
    return [by_id[i] for i in ids if i in by_id]


def makeup_schedules(scope: ScopeState, active=None) -> list[Schedule]:
    """Makeup schedules exactly as run_scope includes them."""
    return _makeup_schedules_for_scope(active if active is not None else active_schedules(scope))


def all_scope_schedules(scope: ScopeState) -> list[Schedule]:
    return list(Schedule.objects.filter(account_id=scope.account_id, channel=scope.channel,
                                        month=scope.month).order_by('schedule_number', '-version'))


def has_commercial_rows(scope: ScopeState) -> bool:
    """Amendment P1: mirrors the condition under which run_scope raises
    ValueError('No schedule rows found …') — scope rows plus makeup rows."""
    if ScheduleRow.objects.filter(account_id=scope.account_id, channel=scope.channel,
                                  month=scope.month, ad_type=COMMERCIAL).exists():
        return True
    mk = makeup_schedules(scope)
    return bool(mk) and ScheduleRow.objects.filter(schedule__in=mk, ad_type=COMMERCIAL).exists()


def period(scope: ScopeState, active=None):
    active = active if active is not None else active_schedules(scope)
    starts = [s.start_date for s in active if s.start_date]
    ends = [s.end_date for s in active if s.end_date]
    if not starts or not ends:
        agg = ScheduleRow.objects.filter(schedule__in=active).aggregate(a=Min('date'), b=Max('date'))
        starts = starts or ([agg['a']] if agg['a'] else [])
        ends = ends or ([agg['b']] if agg['b'] else [])
    return (min(starts) if starts else None, max(ends) if ends else None)
