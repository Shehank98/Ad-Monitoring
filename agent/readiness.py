"""
Scope readiness (AGENT_BUILD_BRIEF.md §7, Amendment A3 V4 / A4). Read-only.

This is the single source of scope state for the agent and the preview pages.
Order of evaluation:
  1. no active schedule          -> STANDALONE (TC without schedule) / WAITING_INPUTS(no_schedule)
  2. authorised                  -> AUTHORISED (frozen for the agent)
  3. V4 active-set sanity        -> NEEDS_HUMAN (duplicate_active_number | schedule_locked)
  4. period not ended (+grace)   -> STILL_AIRING
  5. LMRB coverage (L)           -> WAITING_INPUTS(no_lmrb | lmrb_partial)
  6. TC per active schedule (T)  -> WAITING_INPUTS(tc_not_linked | no_tc)
  7. unmapped brands             -> MAPPING
  8. TC rows untouched           -> RECONCILING(not_reconciled)
  9. otherwise                   -> READY_FOR_SIGNOFF
Rule 8 rows (dated after the latest LMRB date) are "waiting", never a failure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.db.models import Max, Q

from core.models import LMRBRow, ScheduleRow, SummaryReportMeta, TCRow, TransmissionReport
from verification.engine import _lmrb_channel_q

from . import checks
from .models import AgentAuthorisation, AgentConfig, ScopeState
from .scope import active_schedules, period

COMMERCIAL = 'COMMERCIAL BENEFITS'

REASON_LABEL = {
    'no_schedule': 'No schedule in this scope',
    'no_lmrb': 'No LMRB data for this channel',
    'lmrb_partial': 'LMRB data stops before the period ends',
    'no_tc': 'TC not received',
    'tc_not_linked': 'TC uploaded but not linked to a schedule',
    'duplicate_active_number': 'Two active schedules share a schedule number',
    'schedule_locked': 'A schedule in this scope is locked',
    'not_reconciled': 'TC uploaded but reconciliation has not run',
    'no_commercial_rows': 'No commercial rows in this scope',
}


@dataclass
class ScheduleReadiness:
    schedule: object
    has_tc: bool
    rows: int
    matched: int
    pending: int
    pending_after_lmrb: int      # Rule 8: waiting, not failure

    @property
    def sub_status(self) -> str:
        if self.pending_after_lmrb and self.pending_after_lmrb == self.pending:
            return 'pending_rows'
        return 'reconciled' if self.has_tc and not self.pending else 'waiting'


@dataclass
class Readiness:
    state: str
    reason: str = ''
    start: date | None = None
    end: date | None = None
    lmrb_until: date | None = None
    schedules: list = field(default_factory=list)       # list[ScheduleReadiness]
    unmapped: list = field(default_factory=list)        # [(brand, duration)]
    wildcard_only: list = field(default_factory=list)   # [(brand, duration)]
    warnings: list = field(default_factory=list)        # [(code, text)]
    authorised_by: str = ''

    @property
    def reason_label(self):
        return REASON_LABEL.get(self.reason, self.reason)


def _schedule_readiness(sched, lmrb_until) -> ScheduleReadiness:
    rows = ScheduleRow.objects.filter(schedule=sched, ad_type=COMMERCIAL)
    n = rows.count()
    matched = rows.filter(is_matched=True).count() + rows.filter(is_manual_matched=True, is_matched=False).count()
    unmatched = rows.filter(is_matched=False, is_manual_matched=False)
    after = unmatched.filter(date__gt=lmrb_until).count() if lmrb_until else 0
    return ScheduleReadiness(
        schedule=sched,
        has_tc=TransmissionReport.objects.filter(schedule=sched).exists(),
        rows=n, matched=matched, pending=n - matched, pending_after_lmrb=after,
    )


def assess(scope: ScopeState, today: date | None = None, tc_theme_map=None) -> Readiness:
    today = today or date.today()
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    active = active_schedules(scope)

    if not active:
        standalone = TransmissionReport.objects.filter(account_id=acc, channel=ch, month=mo).exists()
        return Readiness('STANDALONE' if standalone else 'WAITING_INPUTS',
                         '' if standalone else 'no_schedule')

    r = Readiness('READY_FOR_SIGNOFF')
    r.start, r.end = period(scope, active)
    r.lmrb_until = LMRBRow.objects.filter(_lmrb_channel_q(ch), account_id=acc,
                                          source='mediawatch').aggregate(d=Max('date'))['d']
    r.schedules = [_schedule_readiness(s, r.lmrb_until) for s in active]
    r.unmapped, r.wildcard_only = checks.unmapped_pairs(acc, [s.id for s in active], tc_theme_map)

    widths = checks.mixed_width_numbers(acc, ch, mo)
    if widths:
        r.warnings.append(('SCHEDULE_NUMBER_WIDTH',
                           f'Schedule numbers have different widths ({", ".join(widths)}); '
                           'the engine orders them as text.'))

    # 2. Authorised: a person authorised it in the core Summary page, or every active
    #    schedule has an AgentAuthorisation.
    meta = SummaryReportMeta.objects.filter(account_id=acc, channel=ch, month=mo).first()
    if meta and meta.authorised_by.strip():
        r.state, r.authorised_by = 'AUTHORISED', meta.authorised_by.strip()
        return r
    if all(AgentAuthorisation.objects.filter(schedule=s).exists() for s in active):
        r.state = 'AUTHORISED'
        return r

    # 3. V4 active-set sanity
    if checks.duplicate_active_numbers(acc, ch, mo):
        r.state, r.reason = 'NEEDS_HUMAN', 'duplicate_active_number'
        return r
    if checks.locked_schedules(acc, ch, mo):
        r.state, r.reason = 'NEEDS_HUMAN', 'schedule_locked'
        return r

    # 4. Period ended (+ grace days)
    cfg = AgentConfig.objects.filter(pk=1).first()     # read-only: never creates the row
    grace = cfg.grace_days if cfg else AgentConfig._meta.get_field('grace_days').default
    if r.end and today <= r.end + timedelta(days=grace):
        r.state = 'STILL_AIRING'
        return r

    # 5. L
    if not r.lmrb_until:
        r.state, r.reason = 'WAITING_INPUTS', 'no_lmrb'
        return r
    if r.end and r.lmrb_until < r.end:
        r.state, r.reason = 'WAITING_INPUTS', 'lmrb_partial'
        return r

    # 6. T
    if any(not s.has_tc for s in r.schedules):
        unlinked = TransmissionReport.objects.filter(account_id=acc, channel=ch, month=mo,
                                                     schedule__isnull=True).exists()
        r.state, r.reason = 'WAITING_INPUTS', ('tc_not_linked' if unlinked else 'no_tc')
        return r

    # 7. Mapping
    if r.unmapped:
        r.state = 'MAPPING'
        return r

    # 8. Reconciled?
    tc_rows = TCRow.objects.filter(tc_report__schedule__in=active)
    touched = Q(is_schedule_matched=True) | Q(is_extra=True) | Q(is_lmrb_confirmed=True)
    if tc_rows.exists() and not tc_rows.filter(touched).exists():
        r.state, r.reason = 'RECONCILING', 'not_reconciled'
        return r

    return r

