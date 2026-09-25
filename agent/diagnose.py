"""
Diagnoser (brief §9 diagnose, Amendment A5). Information only: nothing here writes,
and nothing is auto-fixed. Each Finding says what a person (or a later-phase proposal)
could do, and the tier that fix would need.

The CLAUDE.md §16 symptom table is encoded as the NO_TC_MAPPING, TC_NO_ROWS,
LMRB_THEME_NO_ROWS and SPONSORSHIP_NOT_RUN checks; the A5 findings follow.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from core.models import (
    BrandMapping, ScheduleRow, SponsorshipLmrbAssignment, TransmissionReport,
)
from verification.tc_engine import _build_lmrb_theme_map, _lmrb_themes_for_brand

from . import checks
from .models import ScopeState
from .readiness import assess
from .scope import active_schedules, period

COMMERCIAL = 'COMMERCIAL BENEFITS'


@dataclass
class Finding:
    code: str
    text: str
    tier: int = 4            # tier any fix would need (brief §8); 0 = information only
    brand: str = ''
    sub_code: str = ''
    evidence: dict = field(default_factory=dict)
    proposed_fix: str = ''
    severity: str = 'warn'   # info | warn | bad

    def as_dict(self):
        return asdict(self)


def diagnose(scope: ScopeState, readiness=None) -> list[Finding]:
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    r = readiness or assess(scope)
    active = active_schedules(scope)
    ids = [s.id for s in active]
    start, end = period(scope, active)
    out: list[Finding] = []

    # ── CLAUDE.md §16 symptoms ──
    for brand, dur in r.unmapped:
        out.append(Finding('NO_TC_MAPPING', f'{brand} ({dur}s) has no tc_theme for this duration; '
                           'it counts as Missed until mapped.', tier=3, brand=brand, severity='bad',
                           evidence={'duration': dur},
                           proposed_fix='Add the exact TC theme (no wildcard) to BrandMapping.tc_theme.'))
    for tr in TransmissionReport.objects.filter(schedule_id__in=ids, row_count=0):
        out.append(Finding('TC_NO_ROWS', f'TC file "{tr.original_filename}" produced 0 rows.', tier=2,
                           severity='bad', evidence={'tc_report_id': tr.id},
                           proposed_fix='Check the column names; propose an extra alias in System Settings.'))
    lmrb_map = _build_lmrb_theme_map(acc)
    brands = (ScheduleRow.objects.filter(schedule_id__in=ids, ad_type=COMMERCIAL)
              .values_list('brand', 'duration').order_by().distinct())
    for brand, dur in brands:
        themes = _lmrb_themes_for_brand(brand, dur, lmrb_map)
        if not themes or not start:
            continue
        pat_q = None
        from django.db.models import Q
        for t, _p in themes:
            q = Q(advt_theme__istartswith=t[:-1]) if t.endswith('*') else Q(advt_theme__iexact=t)
            pat_q = q if pat_q is None else pat_q | q
        if not checks.lmrb_scope_qs(acc, ch, start, end).filter(pat_q).exists():
            out.append(Finding('LMRB_THEME_NO_ROWS', f'No LMRB row matches the LMRB theme mapped to {brand}.',
                               tier=3, brand=brand, evidence={'duration': dur},
                               proposed_fix='Check BrandMapping.theme spelling against Advt_Theme.'))
    spon = ScheduleRow.objects.filter(schedule_id__in=ids, ad_type='SPONSORSHIP')
    if spon.exists() and not SponsorshipLmrbAssignment.objects.filter(schedule_row__in=spon).exists():
        out.append(Finding('SPONSORSHIP_NOT_RUN', 'Sponsorship rows exist but none has an LMRB assignment.',
                           tier=1, proposed_fix='Run sponsorship reconciliation (smart).'))

    # ── Amendment A5 findings ──
    for brand, dur in r.wildcard_only:
        out.append(Finding('WILDCARD_TC_THEME_COMMERCIAL',
                           f'{brand} ({dur}s) is mapped only by a wildcard tc_theme; the commercial '
                           'summary counts it as 0.', tier=4, brand=brand, severity='bad',
                           proposed_fix='Append the exact TC theme values with "|" instead of "*".'))
    for group in checks.channel_variants(acc):
        if ch in group:
            out.append(Finding('CHANNEL_VARIANT', 'Channel strings differ only by case or whitespace: '
                               + ', '.join(f'"{g}"' for g in group), tier=4, evidence={'variants': group}))
    widths = checks.mixed_width_numbers(acc, ch, mo)
    if widths:
        out.append(Finding('SCHEDULE_NUMBER_WIDTH', 'Schedule numbers have different widths; the engine '
                           'orders them as text.', tier=0, evidence={'numbers': widths}, severity='info'))
    for num in checks.duplicate_active_numbers(acc, ch, mo):
        out.append(Finding('DUPLICATE_ACTIVE_NUMBER', f'Two non-superseded schedules share #{num}.',
                           tier=4, severity='bad', evidence={'schedule_number': num}))
    for sid in checks.locked_schedules(acc, ch, mo):
        out.append(Finding('SCHEDULE_LOCKED', 'A schedule in this scope is locked; the agent freezes the scope.',
                           tier=4, evidence={'schedule_id': sid}))
    lost = checks.manual_lock_lost(acc).filter(channel=ch, month=mo)
    if lost.exists():
        out.append(Finding('MANUAL_LOCK_LOST', f'{lost.count()} ManualMatch(es) point at rows that lost '
                           'is_manual_matched.', tier=4, severity='bad',
                           evidence={'manual_match_ids': list(lost.values_list('id', flat=True)[:20])}))
    tb = checks.time_belt_unattributed(acc, ch, mo)
    if tb:
        out.append(Finding('TIME_BELT_UNATTRIBUTED', f'{len(tb)} TC rows are schedule-matched without a '
                           'mapping (time-belt fallback). They are not evidence of attribution.',
                           tier=0, severity='info', evidence={'tc_row_ids': tb[:20]}))
    n_sup = checks.superseded_count(acc, ch, mo)
    if n_sup:
        out.append(Finding('SUPERSEDED_ROWS_PRESENT', f'{n_sup} older schedule version(s) exist; summaries are '
                           'built per active schedule.', tier=0, severity='info', evidence={'count': n_sup}))
    if start:
        n = checks.multi_flag_count(acc, ch, start, end)
        if n:
            out.append(Finding('LMRB_MULTI_FLAG', f'{n} LMRB rows carry more than one lock flag.',
                               tier=0, severity='warn', evidence={'count': n}))

    # ── MAKEUP_LINKED (D25, information only) ──
    for link in checks.makeup_links(schedule_ids=ids):
        numbers = ', '.join(m['makeup_number'] for m in link['makeups'])
        out.append(Finding('MAKEUP_LINKED',
                           f"Schedule #{link['parent_number']} has makeup schedule(s) #{numbers}. "
                           'Each is reconciled and reported in its own scope; the billing rule '
                           'is pending with finance (D25).', tier=0, severity='info', evidence=link))

    # ── BASELINE (Phase 1.2, information only; grouped on the overview) ──
    from .models import ScheduleStatus
    for st in (ScheduleStatus.objects.filter(schedule_id__in=ids).exclude(baseline_reason='')
               .select_related('schedule').order_by('schedule__schedule_number')):
        out.append(Finding('BASELINE', f'Schedule #{st.schedule.schedule_number}: '
                           f'{checks.BASELINE_LABELS.get(st.baseline_reason, st.baseline_reason)}. '
                           'This run becomes the baseline; later changes are checked against it.',
                           tier=0, severity='info', sub_code=st.baseline_reason,
                           evidence={'schedule_id': st.schedule_id, 'reason': st.baseline_reason}))

    # ── LOCK_ORPHANED, per flag (information only) ──
    for sub, sets in checks.lock_orphaned_querysets(acc).items():
        for label, qs in sets:
            if label == 'LMRBRow' and start:
                qs = qs.filter(pk__in=checks.lmrb_scope_qs(acc, ch, start, end).values('pk'))
            elif label == 'ScheduleRow':
                qs = qs.filter(schedule_id__in=ids)
            elif label == 'TCRow':
                qs = qs.filter(channel=ch, tc_report__month=mo)
            else:
                continue
            n = qs.count()
            if n:
                out.append(Finding('LOCK_ORPHANED', f'{n} {label} row(s) carry a {sub} lock with no record '
                                   'behind it.', tier=0, sub_code=sub, severity='warn',
                                   evidence={'model': label, 'ids': list(qs.values_list('id', flat=True)[:20])}))
    return out
