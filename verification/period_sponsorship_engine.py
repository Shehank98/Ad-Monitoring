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
    ScheduleRow, parse_channel_media_type,
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
    → one PeriodSponsorship with start=min date, end=max date,
    planned_count=row count. Groups that already have a 'schedule' entry for the
    same (brand, duration) are skipped so re-running does not duplicate.
    """
    rows = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month, ad_type='SPONSORSHIP',
    ).values('brand', 'duration', 'date')

    groups: dict = {}
    for r in rows:
        key = (r['brand'], r['duration'])
        g = groups.setdefault(key, {'dates': [], 'count': 0})
        if r['date']:
            g['dates'].append(r['date'])
        g['count'] += 1

    existing = {
        (ps.brand, ps.duration)
        for ps in PeriodSponsorship.objects.filter(
            account_id=account_id, channel=channel, month=month, source='schedule',
        )
    }

    created = 0
    for (brand, duration), g in groups.items():
        if (brand, duration) in existing or not g['dates']:
            continue
        PeriodSponsorship.objects.create(
            account_id=account_id, channel=channel, month=month,
            brand=brand, duration=duration,
            start_date=min(g['dates']), end_date=max(g['dates']),
            planned_count=g['count'], source='schedule', created_by=user,
        )
        created += 1

    return {'created': created, 'groups': len(groups)}
