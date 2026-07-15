"""
Period (date-range) Sponsorship Engine.

Some sponsorships never appear as a matchable spot in the TC (e.g. a logo shown
during a programme for a whole month), so the spot-based sponsorship engine
cannot pair them one-to-one. Instead a PeriodSponsorship is defined over a
start_date → end_date and verified by COUNTING LMRB appearances of its theme in
that window ("coverage count"). Each counted LMRB row is locked
(is_sponsorship_matched=True) so it can never be double-counted by the
commercial, spot-sponsorship, TC↔LMRB or manual engines.

Public API
----------
reconcile_period_sponsorship(ps, user=None)
    Count + lock LMRB appearances for one PeriodSponsorship (idempotent: keeps
    existing matches, only claims still-free rows). Returns coverage dict.

reset_period_sponsorship(ps)
    Delete the PeriodSponsorship's matches and unlock their LMRB rows.

delete_period_sponsorship(ps)
    Unlock rows, then delete the PeriodSponsorship (cascades matches).

import_from_schedule(account_id, channel, month, user=None)
    Create 'schedule'-sourced PeriodSponsorships by grouping SPONSORSHIP
    ScheduleRows per (brand, duration): start=min date, end=max date,
    planned_count=row count. Skips groups already imported.

coverage(ps)
    Return the current coverage dict for a PeriodSponsorship without changing
    anything (found/planned/matched rows), for display and Excel export.
"""
import logging

from django.db import transaction
from django.db.models import Q

from core.models import (
    BrandMapping, LMRBRow, PeriodSponsorship, PeriodSponsorshipMatch,
    Schedule, ScheduleRow, parse_channel_media_type,
)

logger = logging.getLogger(__name__)


def _normalize(s) -> str:
    return str(s).lower().strip() if s else ''


def _channel_forms(channel: str):
    """Match both 'TV - X' and 'X' channel spellings."""
    _, clean = parse_channel_media_type(channel)
    forms = {channel}
    if clean:
        forms.add(clean)
    return forms


def _target_themes(ps: PeriodSponsorship):
    """Normalised LMRB themes this period sponsorship should count.

    If ps.theme is set, use it directly; otherwise resolve ps.brand → LMRB
    themes via BrandMapping.theme (respecting ps.duration when the mapping row
    pins a duration). Returns a list of normalised theme strings ('*' preserved
    for prefix matching).
    """
    if ps.theme and ps.theme.strip():
        return [_normalize(ps.theme)]
    dur = int(ps.duration) if ps.duration is not None else None
    out = []
    for bm in BrandMapping.objects.filter(account_id=ps.account_id).exclude(theme=''):
        if _normalize(bm.brand) != _normalize(ps.brand):
            continue
        if bm.duration is not None and dur is not None and int(bm.duration) != dur:
            continue
        out.append(_normalize(bm.theme))
    # de-dup preserving order
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _lmrb_matches_theme(advt_theme: str, targets) -> bool:
    lt = _normalize(advt_theme)
    for t in targets:
        if t.endswith('*'):
            if lt.startswith(t[:-1]):
                return True
        elif lt == t:
            return True
    return False


def _free_lmrb_pool(ps: PeriodSponsorship):
    """LMRB rows in scope that no engine has claimed yet."""
    cq = Q()
    for f in _channel_forms(ps.channel):
        cq |= Q(channel__iexact=f)
    qs = LMRBRow.objects.filter(
        cq, account_id=ps.account_id,
        date__gte=ps.start_date, date__lte=ps.end_date,
        is_matched=False,
        is_sponsorship_matched=False,
        is_manual_matched=False,
        is_tc_lmrb_matched=False,
    )
    if ps.duration is not None:
        qs = qs.filter(duration=ps.duration)
    return qs.order_by('date', 'advt_time')


@transaction.atomic
def reconcile_period_sponsorship(ps: PeriodSponsorship, user=None) -> dict:
    """Count + lock LMRB appearances for one PeriodSponsorship.

    Idempotent: existing matches are kept; only still-free LMRB rows that match
    the target theme(s) in the date window are newly claimed and locked.
    """
    targets = _target_themes(ps)
    new_matches, lock_ids = [], []
    if targets:
        for lr in _free_lmrb_pool(ps):
            if _lmrb_matches_theme(lr.advt_theme, targets):
                new_matches.append(PeriodSponsorshipMatch(period_sponsorship=ps, lmrb_row=lr))
                lock_ids.append(lr.id)

    if new_matches:
        PeriodSponsorshipMatch.objects.bulk_create(new_matches)
        LMRBRow.objects.filter(id__in=lock_ids).update(is_sponsorship_matched=True)

    logger.info('reconcile_period_sponsorship: ps=%s brand=%s newly_locked=%d',
                ps.id, ps.brand, len(new_matches))
    return coverage(ps)


@transaction.atomic
def reset_period_sponsorship(ps: PeriodSponsorship) -> int:
    """Delete this period sponsorship's matches and unlock their LMRB rows."""
    lmrb_ids = list(ps.matches.values_list('lmrb_row_id', flat=True))
    ps.matches.all().delete()
    if lmrb_ids:
        LMRBRow.objects.filter(id__in=lmrb_ids).update(is_sponsorship_matched=False)
    return len(lmrb_ids)


@transaction.atomic
def delete_period_sponsorship(ps: PeriodSponsorship) -> None:
    """Unlock rows then delete the PeriodSponsorship (cascades its matches)."""
    reset_period_sponsorship(ps)
    ps.delete()


def coverage(ps: PeriodSponsorship) -> dict:
    """Current coverage snapshot for a PeriodSponsorship (no side effects)."""
    matches = list(
        ps.matches.select_related('lmrb_row').order_by('lmrb_row__date', 'lmrb_row__advt_time')
    )
    found = len(matches)
    planned = ps.planned_count or 0
    days_covered = len({m.lmrb_row.date for m in matches})
    return {
        'id':           ps.id,
        'brand':        ps.brand,
        'theme':        ps.theme,
        'duration':     ps.duration,
        'start_date':   ps.start_date,
        'end_date':     ps.end_date,
        'planned':      planned,
        'found':        found,
        'short':        max(0, planned - found),
        'extra':        max(0, found - planned),
        'days_covered': days_covered,
        'source':       ps.source,
        'note':         ps.note,
        'matches':      matches,
        'mapped':       bool(_target_themes(ps)),
    }


@transaction.atomic
def import_from_schedule(account_id, channel, month, user=None) -> dict:
    """Create 'schedule'-sourced PeriodSponsorships by grouping SPONSORSHIP rows.

    Groups unimported SPONSORSHIP ScheduleRows for the scope by (brand, duration)
    → one PeriodSponsorship. The date window is the **Schedule header** span, not
    the row dates: when a sponsorship is merged into a schedule its rows can all
    carry a single date, so we use the widest span of the Schedule records that
    contain those rows — min(Schedule.start_date) → max(Schedule.end_date) — with
    the row date min/max as a fallback when a header date is blank. planned_count
    is the row count. Groups already imported (same brand, duration) are skipped.
    """
    rows = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month, ad_type='SPONSORSHIP',
    ).values('brand', 'duration', 'date', 'schedule_id')

    groups: dict = {}
    for r in rows:
        key = (r['brand'], r['duration'])
        g = groups.setdefault(key, {'dates': [], 'count': 0, 'sched_ids': set()})
        if r['date']:
            g['dates'].append(r['date'])
        if r['schedule_id']:
            g['sched_ids'].add(r['schedule_id'])
        g['count'] += 1

    # Schedule header start/end dates for every schedule referenced above.
    all_sched_ids = {sid for g in groups.values() for sid in g['sched_ids']}
    sched_dates = {
        s.id: (s.start_date, s.end_date)
        for s in Schedule.objects.filter(id__in=all_sched_ids)
    }

    existing = {
        (ps.brand, ps.duration)
        for ps in PeriodSponsorship.objects.filter(
            account_id=account_id, channel=channel, month=month, source='schedule',
        )
    }

    created = 0
    for (brand, duration), g in groups.items():
        if (brand, duration) in existing:
            continue
        # Widest span across the Schedule headers containing this group's rows.
        starts = [sched_dates[sid][0] for sid in g['sched_ids']
                  if sched_dates.get(sid) and sched_dates[sid][0]]
        ends = [sched_dates[sid][1] for sid in g['sched_ids']
                if sched_dates.get(sid) and sched_dates[sid][1]]
        start_date = min(starts) if starts else (min(g['dates']) if g['dates'] else None)
        end_date = max(ends) if ends else (max(g['dates']) if g['dates'] else None)
        if not (start_date and end_date):
            continue
        PeriodSponsorship.objects.create(
            account_id=account_id, channel=channel, month=month,
            brand=brand, duration=duration,
            start_date=start_date, end_date=end_date,
            planned_count=g['count'], source='schedule', created_by=user,
        )
        created += 1

    return {'created': created, 'groups': len(groups)}


def unmatched_lmrb_groups(account_id, channel, month=''):
    """Free (unclaimed) LMRB rows in scope grouped by (product, advt_theme, duration).

    Lets the user see what monitoring data is still available and click a group to
    pre-fill the Add form. Scope is the client's LMRB for the channel; when a month
    is given the window is that calendar month, else all of the channel's LMRB.
    Returns a list of dicts sorted by count desc:
      {product, theme, duration, count, date_min, date_max}
    Only rows free of every lock (is_matched / is_sponsorship_matched /
    is_manual_matched / is_tc_lmrb_matched) are included.
    """
    from datetime import datetime as _dt

    cq = Q()
    for f in _channel_forms(channel):
        cq |= Q(channel__iexact=f)
    qs = LMRBRow.objects.filter(
        cq, account_id=account_id,
        is_matched=False, is_sponsorship_matched=False,
        is_manual_matched=False, is_tc_lmrb_matched=False,
    )
    if month:
        try:
            d0 = _dt.strptime(month, '%B %Y').date()
            nm = (d0.month % 12) + 1
            ny = d0.year + (1 if d0.month == 12 else 0)
            from datetime import date as _date, timedelta as _td
            d1 = _date(ny, nm, 1) - _td(days=1)
            qs = qs.filter(date__gte=d0, date__lte=d1)
        except ValueError:
            pass

    groups: dict = {}
    for lr in qs.values('product', 'advt_theme', 'duration', 'date'):
        key = ((lr['product'] or '').strip(), (lr['advt_theme'] or '').strip(), lr['duration'])
        g = groups.setdefault(key, {'count': 0, 'dmin': None, 'dmax': None})
        g['count'] += 1
        d = lr['date']
        if d:
            g['dmin'] = d if g['dmin'] is None or d < g['dmin'] else g['dmin']
            g['dmax'] = d if g['dmax'] is None or d > g['dmax'] else g['dmax']

    out = [
        {'product': p, 'theme': t, 'duration': dur,
         'count': g['count'], 'date_min': g['dmin'], 'date_max': g['dmax']}
        for (p, t, dur), g in groups.items()
        if t  # skip blank themes (nothing to map)
    ]
    out.sort(key=lambda x: (-x['count'], x['theme'].lower(), x['duration'] or 0))
    return out
