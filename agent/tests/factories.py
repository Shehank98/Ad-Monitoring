"""Synthetic data builders for agent tests. Real engines run on this data (never mocked)."""
import datetime

from accounts.models import User
from core.models import (
    Account, BrandMapping, LMRBRow, Schedule, ScheduleRow, TCRow, TransmissionReport,
)

CHANNEL = 'Sirasa TV'
MONTH = 'January 2025'
D = datetime.date


def account(name='Keells'):
    return Account.objects.create(name=name)


def user(role='admin', email=None, accounts=()):
    u = User.objects.create_user(email=email or f'{role}@t.com', name=f'{role} user',
                                 password='password123', role=role, must_change_password=False)
    if accounts:
        u.accounts.set(accounts)
    return u


def schedule(acc, number='101', version=1, channel=CHANNEL, month=MONTH, start=D(2025, 1, 1),
             end=D(2025, 1, 31), superseded=False, locked=False):
    return Schedule.objects.create(
        account=acc, channel=channel, month=month, schedule_number=number, version=version,
        file='schedules/x.xlsx', original_filename='x.xlsx', start_date=start, end_date=end,
        is_superseded=superseded, is_locked=locked)


def row(acc, sched, brand='Nexus', dur=30, day=10, start='20:00:00', end='20:15:00',
        ad_type='COMMERCIAL BENEFITS', programme='News'):
    return ScheduleRow.objects.create(
        schedule=sched, account=acc, channel=sched.channel, month=sched.month, brand=brand,
        programme=programme, date=D(2025, 1, day), start_time=start, end_time=end, duration=dur,
        ad_type=ad_type)


def lmrb(acc, day=10, time='20:05:00', theme='Nexus (30)(Sin)', dur=30, channel=CHANNEL,
         source='mediawatch', **flags):
    d = D(2025, 1, day)
    return LMRBRow.objects.create(
        account=acc, channel=channel, date=d, advt_theme=theme, advt_time=time, duration=dur,
        source=source, dedup_key=LMRBRow.make_dedup_key(acc.id, channel, d, time, theme, dur), **flags)


def mapping(acc, brand='Nexus', theme='Nexus (30)(Sin)', tc='NEXUS 30', dur=None):
    return BrandMapping.objects.create(account=acc, brand=brand, theme=theme, tc_theme=tc, duration=dur)


def tc_report(acc, sched=None, channel=CHANNEL, month=MONTH, rows=0):
    return TransmissionReport.objects.create(
        account=acc, channel=channel, month=month, schedule=sched,
        file='tc/x.xlsx', original_filename='x.xlsx', row_count=rows)


def tc_row(acc, report, day=10, time='20:05:02', theme='NEXUS 30', dur=30, **flags):
    d = D(2025, 1, day)
    return TCRow.objects.create(
        account=acc, tc_report=report, channel=report.channel, date=d, tc_theme=theme,
        duration=dur, aired_time=time,
        dedup_key=TCRow.make_dedup_key(acc.id, report.channel, d, time, theme, dur), **flags)


def full_scope(acc=None, number='101', brand='Nexus', days=(10, 12), channel=CHANNEL):
    """A complete, mapped scope: schedule + rows + LMRB (to 31 Jan) + linked TC + TC rows."""
    acc = acc or account()
    s = schedule(acc, number=number, channel=channel)
    for d in days:
        row(acc, s, brand=brand, day=d)
        lmrb(acc, day=d, theme=f'{brand} (30)(Sin)', channel=channel)
    lmrb(acc, day=31, time='23:00:00', theme='Other (30)', channel=channel)
    if not BrandMapping.objects.filter(account=acc, brand=brand).exists():
        mapping(acc, brand=brand, theme=f'{brand} (30)(Sin)', tc=f'{brand.upper()} 30')
    rep = tc_report(acc, s, channel=channel, rows=len(days))
    for d in days:
        tc_row(acc, rep, day=d, theme=f'{brand.upper()} 30')
    return acc, s
