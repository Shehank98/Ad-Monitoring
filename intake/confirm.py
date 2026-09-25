"""
Admin Confirm (web process only). The ONLY intake code that writes a TransmissionReport,
its TCRows and its file (owner C, C4; Amendment A6).

  eligible_schedules(att)      dropdown: active, not locked, not authorised, no duplicate
                               active number (exact strings shown)
  confirm_upload(att, schedule, admin, dates_ack=False)
      1. refuses: schedule_frozen / schedule_locked / duplicate_active_number /
         no_schedule (not active) / date_out_of_range unless the second tick box is set
      2. gate.perform(actor_kind='human', actor=admin) -> AgentAction human_confirmed=True
         (not blocked by the kill switch; owner Q6)
      3. TransmissionReport: account, channel, month, schedule copied from the Schedule;
         start/end from _detect_tc_meta; uploaded_by = the admin
      4. A6 collision guard: record every existing TCRow id of the account+channel, parse
         inside a savepoint; if any recorded id disappeared, everything is rolled back
         (no report, no file) and an AgentProposal records the conflict
      5. the file is saved through Django default storage only after the guard passed
      6. ScopeState.needs_run = True. Never reconciles.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from agent import gate
from agent.models import AgentAuthorisation, AgentConfig, AgentProposal, ScopeState
from core.models import Schedule, SummaryReportMeta, TCRow, TransmissionReport
from core.views import _detect_tc_meta, _parse_tc_rows
from verification.engine import active_schedule_ids

from .cron.tools import parse_attachment

REFUSALS = ('schedule_frozen', 'schedule_locked', 'duplicate_active_number', 'no_schedule',
            'date_out_of_range', 'tc_already_exists', 'too_large', 'columns_unrecognised')


class ConfirmRefused(Exception):
    def __init__(self, reason, detail=''):
        super().__init__(f'{reason}: {detail}' if detail else reason)
        self.reason, self.detail = reason, detail


class Collision(Exception):
    def __init__(self, reason, gone_ids):
        super().__init__(reason)
        self.reason, self.gone_ids = reason, gone_ids


def _legacy_authorised(s: Schedule) -> bool:
    """Scope signed off the existing way (SummaryReportMeta.authorised_by), as readiness.py does."""
    return SummaryReportMeta.objects.filter(account_id=s.account_id, channel=s.channel, month=s.month) \
        .exclude(authorised_by='').exists()


def schedule_problems(s: Schedule) -> list[str]:
    out = []
    if AgentAuthorisation.objects.filter(schedule=s).exists() or _legacy_authorised(s):
        out.append('schedule_frozen')
    if s.is_locked:
        out.append('schedule_locked')
    same = Schedule.objects.filter(account_id=s.account_id, channel=s.channel, month=s.month,
                                   schedule_number=s.schedule_number, is_superseded=False).count()
    if same > 1:
        out.append('duplicate_active_number')
    if s.id not in set(active_schedule_ids(s.account_id, s.channel, s.month)):
        out.append('no_schedule')
    return out


def eligible_schedules(att=None, account_ids=None):
    """Schedules an admin may confirm to, candidates for this attachment first."""
    qs = Schedule.objects.filter(is_superseded=False, is_locked=False).select_related('account')
    if account_ids is not None:
        qs = qs.filter(account_id__in=account_ids)
    first = {att.suggested_schedule_id, att.llm_hint_schedule_id} - {None} if att else set()
    cands = set(((att.evidence or {}).get('candidates') or {}).keys()) if att else set()
    rows = [s for s in qs.order_by('account__name', 'channel', 'month', 'schedule_number')
            if not schedule_problems(s)]
    return sorted(rows, key=lambda s: (s.id not in first, str(s.id) not in cands))


def label(s: Schedule) -> str:
    return f'{s.account.name} | {s.channel} | {s.month} | #{s.schedule_number} v{s.version}'


def _window(s):
    cfg = AgentConfig.objects.filter(pk=1).first()
    grace = cfg.grace_days if cfg else AgentConfig._meta.get_field('grace_days').default
    return s.start_date, (s.end_date + timedelta(days=grace)) if s.end_date else None


def confirm_upload(att, schedule: Schedule, admin, dates_ack: bool = False) -> dict:
    problems = schedule_problems(schedule)
    if problems:
        raise ConfirmRefused(problems[0])
    if att.tc_report_id:
        raise ConfirmRefused('tc_already_exists', 'this attachment was already uploaded')
    data = bytes(att.content or b'')
    if not data:
        raise ConfirmRefused('too_large', 'no stored content (too large or purged)')
    parsed = parse_attachment(att, channel_for_pdf=schedule.channel)
    df = parsed['df']
    if df is None or df.empty:
        raise ConfirmRefused('columns_unrecognised', 'no rows could be read')
    df = df.copy()
    meta = _detect_tc_meta(df)
    start, end = _window(schedule)
    out_of_window = bool(meta['start_date'] and start and end and
                         (meta['start_date'] < start or meta['end_date'] > end))
    if out_of_window and not dates_ack:
        raise ConfirmRefused('date_out_of_range',
                             f"TC {meta['start_date']}..{meta['end_date']} vs {start}..{end}")

    def apply():
        with transaction.atomic():
            protected = set(TCRow.objects.filter(account_id=schedule.account_id, channel=schedule.channel)
                            .values_list('id', flat=True))
            same_schedule = set(TCRow.objects.filter(id__in=protected, tc_report__schedule=schedule)
                                .values_list('id', flat=True))
            rep = TransmissionReport.objects.create(
                account=schedule.account, channel=schedule.channel, month=schedule.month,
                schedule=schedule, file='', original_filename=att.filename, row_count=0,
                start_date=meta['start_date'], end_date=meta['end_date'], uploaded_by=admin)
            sid = transaction.savepoint()
            count = _parse_tc_rows(df, schedule.account, rep)
            still = set(TCRow.objects.filter(id__in=protected).values_list('id', flat=True))
            gone = sorted(protected - still)
            if gone:
                transaction.savepoint_rollback(sid)
                raise Collision('tc_already_exists' if set(gone) & same_schedule else 'conflict', gone)
            transaction.savepoint_commit(sid)
            rep.row_count = count
            rep.file.save(att.filename, ContentFile(data), save=False)   # default storage, as core
            rep.save(update_fields=['row_count', 'file'])
            att.status, att.reason, att.tc_report = 'uploaded', '', rep
            att.suggested_schedule = schedule
            att.decided_at = timezone.now()
            att.evidence = {**(att.evidence or {}), 'confirmed': {
                'by': admin.email, 'schedule_id': schedule.id, 'dates_outside_window_ticked': out_of_window}}
            att.save()
            sc.needs_run = True                               # intake never reconciles
            sc.save(update_fields=['needs_run', 'updated_at'])
            return {'tc_report_id': rep.id, 'rows': count, 'schedule_id': schedule.id,
                    'channel': rep.channel, 'month': rep.month, 'file': rep.file.name}

    # Agent table only: the scope this upload belongs to (strings copied from the Schedule)
    sc, _ = ScopeState.objects.get_or_create(account=schedule.account, channel=schedule.channel,
                                             month=schedule.month)
    try:
        act = gate.perform(
            tier=gate.T1, action_type='intake_confirm_upload', actor_kind='human', actor=admin, scope=sc,
            account_id=schedule.account_id, target_model='core.TransmissionReport',
            target_pk=att.id, before={'attachment_id': att.id, 'status': att.status},
            conditions={'schedule_ok': True, 'dates_ok_or_ticked': (not out_of_window) or dates_ack},
            apply=apply, reason=f'admin confirmed TC upload to schedule #{schedule.schedule_number}',
            evidence={'dates_outside_window_ticked': out_of_window})
    except Collision as c:
        AgentProposal.objects.create(
            kind='tc_link', tier=gate.T4, action_type='intake_collision', schedule=schedule,
            target_model='intake.InboundAttachment', target_pk=str(att.id), actor=admin,
            before={'attachment_id': att.id}, after={},
            reason=f'Upload would replace {len(c.gone_ids)} existing TC row(s): {c.reason}. Rolled back.',
            evidence={'reason': c.reason, 'replaced_tc_row_ids': c.gone_ids[:200]})
        att.status, att.reason = 'needs_review', c.reason
        att.save(update_fields=['status', 'reason'])
        raise ConfirmRefused(c.reason, f'{len(c.gone_ids)} existing TC row(s) would be replaced') from None
    return {**act.after, 'action_id': act.id}


def decide(att, admin, status: str, note: str = '') -> None:
    """Admin Ignore / Reject (intake table only; logged, human_confirmed)."""
    assert status in ('ignored', 'rejected')

    def apply():
        att.status, att.decided_at = status, timezone.now()
        att.note = (note or att.note)[:300]
        att.save(update_fields=['status', 'decided_at', 'note'])
        return {'status': status}
    gate.perform(tier=gate.T0, action_type=f'intake_{status}', actor_kind='human', actor=admin,
                 target_model='intake.InboundAttachment', target_pk=att.id,
                 before={'status': att.status}, apply=apply, reason=note or status)
