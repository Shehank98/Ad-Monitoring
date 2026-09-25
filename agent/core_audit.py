"""
Read-only core audit (Amendment A10, P4).

Measures how many clients/months/schedules are affected by the existing-system issues
found in Phase 0. Strictly read-only: the whole collection runs inside a transaction
that is always rolled back, and on PostgreSQL the transaction is first set READ ONLY.
Nothing in this module calls save(), update(), delete() or bulk_*.

AUDIT EXEMPTION (numbers-guardian check 4): these queries inspect lock flags on purpose.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from django.db import connection, transaction

from core.models import Account, LMRBRow, Schedule, ScheduleRow

from . import checks

COMMERCIAL = 'COMMERCIAL BENEFITS'


class _Rollback(Exception):
    pass


def _month_of(d) -> str:
    return d.strftime('%B %Y') if d else '(no date)'


def collect() -> dict:
    """Return the audit data. Always rolled back; READ ONLY on PostgreSQL."""
    box = {}
    try:
        with transaction.atomic():
            if connection.vendor == 'postgresql':
                with connection.cursor() as cur:
                    cur.execute('SET TRANSACTION READ ONLY')
            box['data'] = _collect()
            raise _Rollback
    except _Rollback:
        pass
    return box['data']


def _collect() -> dict:
    names = dict(Account.objects.values_list('id', 'name'))
    sections = {}

    # 1. Wildcard tc_theme on commercial brands (+ scopes they affect)
    rows = []
    for acc_id in names:
        for bm in checks.wildcard_tc_mappings(acc_id):
            scopes = sorted(set(ScheduleRow.objects.filter(
                account_id=acc_id, ad_type=COMMERCIAL, brand__iexact=bm.brand.strip())
                .values_list('channel', 'month')))
            for ch, mo in scopes or [('—', '—')]:
                rows.append({'account': names[acc_id], 'month': mo, 'channel': ch, 'brand': bm.brand,
                             'example_ids': [bm.id], 'tc_theme': bm.tc_theme})
    sections['wildcard_tc_theme_commercial'] = rows

    # 2..4 schedule-set issues per scope
    sup, dup, width, locked = [], [], [], []
    for acc_id, ch, mo in Schedule.objects.order_by().values_list('account_id', 'channel', 'month').distinct():
        n = checks.superseded_count(acc_id, ch, mo)
        if n:
            sids = list(Schedule.objects.filter(account_id=acc_id, channel=ch, month=mo)
                        .order_by('schedule_number', 'version').values_list('id', flat=True))
            sup_rows = ScheduleRow.objects.filter(account_id=acc_id, channel=ch, month=mo).count()
            sup.append({'account': names[acc_id], 'month': mo, 'channel': ch, 'count': n,
                        'rows_in_scope': sup_rows, 'example_ids': sids[:5]})
        for num in checks.duplicate_active_numbers(acc_id, ch, mo):
            dup.append({'account': names[acc_id], 'month': mo, 'channel': ch, 'schedule_number': num,
                        'example_ids': list(Schedule.objects.filter(account_id=acc_id, channel=ch, month=mo,
                                                                    schedule_number=num).values_list('id', flat=True))})
        w = checks.mixed_width_numbers(acc_id, ch, mo)
        if w:
            width.append({'account': names[acc_id], 'month': mo, 'channel': ch, 'numbers': w})
        lk = checks.locked_schedules(acc_id, ch, mo)
        if lk:
            locked.append({'account': names[acc_id], 'month': mo, 'channel': ch, 'count': len(lk),
                           'example_ids': lk[:5]})
    sections['superseded_schedules'] = sup
    sections['duplicate_active_numbers'] = dup
    sections['mixed_width_numbers'] = width
    sections['locked_schedules'] = locked

    # 5. ManualMatch rows whose referenced rows lost is_manual_matched
    mm = defaultdict(list)
    for m in checks.manual_lock_lost().values('id', 'account_id', 'month'):
        mm[(m['account_id'], m['month'])].append(m['id'])
    sections['manual_lock_lost'] = [{'account': names.get(a, a), 'month': mo, 'count': len(ids),
                                     'example_ids': ids[:5]} for (a, mo), ids in sorted(mm.items())]

    # 6. Time-belt TC rows (schedule-matched without mapping evidence)
    tb = []
    from core.models import TCRow
    for acc_id, ch, mo in (TCRow.objects.filter(is_schedule_matched=True)
                           .order_by().values_list('account_id', 'channel', 'tc_report__month').distinct()):
        ids = checks.time_belt_unattributed(acc_id, ch, mo)
        if ids:
            tb.append({'account': names[acc_id], 'month': mo, 'channel': ch, 'count': len(ids),
                       'example_ids': ids[:5]})
    sections['time_belt_unattributed'] = tb

    # 7. Channel strings differing only by case/whitespace
    sections['channel_variants'] = [{'account': names[a], 'variants': g}
                                    for a in names for g in checks.channel_variants(a)]

    # 8. LMRB rows with more than one lock flag
    multi = Counter()
    examples = defaultdict(list)
    for rid, acc_id, d in LMRBRow.objects.filter(checks.multi_flag_q()).values_list('id', 'account_id', 'date'):
        k = (acc_id, _month_of(d))
        multi[k] += 1
        if len(examples[k]) < 5:
            examples[k].append(rid)
    sections['lmrb_multi_flag'] = [{'account': names[a], 'month': mo, 'count': n, 'example_ids': examples[(a, mo)]}
                                   for (a, mo), n in sorted(multi.items())]

    # 9. TransmissionReports with schedule=None in scopes that have schedules
    sections['tc_unlinked_in_scheduled_scopes'] = [
        {'account': names[tr.account_id], 'month': tr.month, 'channel': tr.channel, 'example_ids': [tr.id]}
        for tr in checks.unlinked_tc_in_scheduled_scopes()]

    # 10. LOCK_ORPHANED per sub-code
    orphan = []
    for sub, sets in checks.lock_orphaned_querysets().items():
        for label, qs in sets:
            per = Counter()
            ex = defaultdict(list)
            date_field = 'date'
            for rid, acc_id, d in qs.values_list('id', 'account_id', date_field):
                k = (acc_id, _month_of(d))
                per[k] += 1
                if len(ex[k]) < 5:
                    ex[k].append(rid)
            for (a, mo), n in sorted(per.items()):
                orphan.append({'sub_code': sub, 'model': label, 'account': names[a], 'month': mo,
                               'count': n, 'example_ids': ex[(a, mo)]})
    sections['lock_orphaned'] = orphan
    return {'sections': sections, 'accounts': len(names), 'generated': date.today().isoformat(),
            'database': connection.vendor}


TITLES = [
    ('wildcard_tc_theme_commercial', 'Wildcard tc_theme on commercial brands (commercial summary counts them as 0)'),
    ('superseded_schedules', 'Scopes with superseded schedules (rows counted when no schedule is selected)'),
    ('duplicate_active_numbers', 'Duplicate active schedule numbers (keep_both)'),
    ('mixed_width_numbers', 'Mixed-width schedule numbers (text ordering risk)'),
    ('locked_schedules', 'Locked schedules (is_locked=True)'),
    ('manual_lock_lost', 'ManualMatch rows whose referenced rows lost is_manual_matched'),
    ('time_belt_unattributed', 'TC rows schedule-matched without mapping evidence (time belt)'),
    ('channel_variants', 'Channel strings differing only by case or whitespace'),
    ('lmrb_multi_flag', 'LMRB rows with more than one lock flag'),
    ('tc_unlinked_in_scheduled_scopes', 'TransmissionReports with schedule=None in scopes that have schedules'),
    ('lock_orphaned', 'LOCK_ORPHANED: lock flag set with no record behind it'),
]


def _distinct(rows, key):
    return len({r.get(key) for r in rows if r.get(key) not in (None, '—')})


def render_markdown(data: dict, synthetic: bool = False) -> str:
    s = data['sections']
    prefix = 'SYNTHETIC DATA — ' if synthetic else ''
    out = []
    if synthetic:
        out.append('SYNTHETIC DATA: this report was produced on a synthetic test database, not on production.')
        out.append('')
    out.append(f'# {prefix}Core audit {data["generated"]}')
    out.append('')
    out.append(f'Read-only audit (Amendment A10). Database: {data["database"]}. Accounts: {data["accounts"]}. '
               'The transaction was set READ ONLY (PostgreSQL) and always rolled back.')
    out.append('')
    out.append('## Headline')
    out.append('')
    out.append('| Issue | Rows | Accounts | Months |')
    out.append('|---|---:|---:|---:|')
    for key, title in TITLES:
        rows = s[key]
        n = sum(r.get('count', 1) for r in rows)
        out.append(f'| {title} | {n} | {_distinct(rows, "account")} | {_distinct(rows, "month")} |')
    for key, title in TITLES:
        rows = s[key]
        out.append('')
        out.append(f'## {title}')
        out.append('')
        if not rows:
            out.append('None found.')
            continue
        cols = [c for c in rows[0].keys()]
        out.append('| ' + ' | '.join(cols) + ' |')
        out.append('|' + '---|' * len(cols))
        for r in rows[:200]:
            out.append('| ' + ' | '.join(str(r.get(c, '')).replace('|', '\\|') for c in cols) + ' |')
        if len(rows) > 200:
            out.append(f'\n… {len(rows) - 200} more rows not shown.')
    return '\n'.join(out) + '\n'
