"""
External-change fingerprint (Phase 1.1 decision 3; scoped in Phase 1.2). Read-only.

A canonical snapshot of what people change through the core UI, so V5 can tell "the
numbers moved because someone edited a mapping / uploaded / changed a setting" from
"the numbers moved and nothing explains it".

The snapshot is ACCOUNT-WIDE (field names checked against core/models.py; none missing):
  brand_mappings        BrandMapping: product, brand, theme, tc_theme, maponline_theme, duration
  tc_lmrb_theme_maps    TcLmrbThemeMap: tc_theme, tc_duration, lmrb_theme, lmrb_duration
  manual_matches        ManualMatch: channel, month
  manual_sponsorship    SponsorshipLmrbAssignment (match_type='manual'): the schedule row's channel, month
  period_sponsorships   PeriodSponsorship: channel, month, start_date, end_date, planned_count, theme
  transmission_reports  TransmissionReport: channel, month, schedule_id, uploaded_at, row_count
  schedules             Schedule: channel, month, version, is_superseded, is_locked
  monitoring_uploaded_max   max MonitoringData.uploaded_at (account; information only)
  settings              via get_setting: tolerance, every tc_extra_*/lmrb_extra_* alias,
                        lmrb_sponsorship_keywords
plus three SCOPE parts (Phase 1.2 item 4):
  monitoring_data       sorted MonitoringData ids for the account and the scope's channel
                        (engine channel filter _lmrb_channel_q)
  lmrb_count            LMRBRow count for the scope's channel and date range
  match_results_run_max max MatchResult.run_at for the scope

The whole diff is kept in AgentRun.detail for information, but only its SCOPE-RELEVANT
part can explain a change (Phase 1.2 item 3, see relevant_diff()).
"""
from __future__ import annotations

from django.db.models import Max

from core.models import (
    BrandMapping, ManualMatch, MatchResult, MonitoringData, PeriodSponsorship, Schedule,
    ScheduleRow, SponsorshipLmrbAssignment, TCRow, TcLmrbThemeMap, TransmissionReport,
    get_setting,
)

from verification.engine import _lmrb_channel_q

from .canonical import sha256_of, to_jsonable

VERSION = 2          # 1 = Phase 1.1 layout (id lists, no scope parts)

ALIAS_KEYS = [
    'tc_extra_theme_aliases', 'tc_extra_time_aliases', 'tc_extra_date_aliases',
    'tc_extra_duration_aliases', 'tc_extra_programme_aliases',
    'lmrb_extra_theme_aliases', 'lmrb_extra_time_aliases', 'lmrb_extra_duration_aliases',
    'lmrb_extra_date_aliases',
]
SETTING_KEYS = ['tc_lmrb_time_tolerance', *ALIAS_KEYS, 'lmrb_sponsorship_keywords']

ROW_SECTIONS = ('brand_mappings', 'tc_lmrb_theme_maps', 'manual_matches', 'manual_sponsorship',
                'period_sponsorships', 'transmission_reports', 'schedules')
ID_SECTIONS = ('monitoring_data',)
SCALAR_SECTIONS = ('monitoring_uploaded_max', 'lmrb_count', 'match_results_run_max')
IN_SCOPE_BY_KEY = ('manual_matches', 'manual_sponsorship', 'period_sponsorships',
                   'transmission_reports', 'schedules')
ALWAYS_RELEVANT = ('monitoring_data', 'lmrb_count', 'match_results_run_max', 'settings')
INFO_ONLY = ('monitoring_uploaded_max',)


def _rows(qs, fields, names=None) -> dict:
    names = names or fields
    return {str(r['id']): {n: r[f] for f, n in zip(fields, names)}
            for r in qs.order_by().values('id', *fields)}


def _norm(s) -> str:
    """The engines' _normalize: lower-case + strip."""
    return str(s).lower().strip() if s else ''


def fingerprint(scope) -> dict:
    # Local import: .scope imports core engines; keep this module import-light.
    from .checks import lmrb_scope_qs
    from .scope import period
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    start, end = period(scope)
    data = {
        'version': VERSION,
        'brand_mappings': _rows(BrandMapping.objects.filter(account_id=acc),
                                ['product', 'brand', 'theme', 'tc_theme', 'maponline_theme', 'duration']),
        'tc_lmrb_theme_maps': _rows(TcLmrbThemeMap.objects.filter(account_id=acc),
                                    ['tc_theme', 'tc_duration', 'lmrb_theme', 'lmrb_duration']),
        'manual_matches': _rows(ManualMatch.objects.filter(account_id=acc), ['channel', 'month']),
        'manual_sponsorship': _rows(
            SponsorshipLmrbAssignment.objects.filter(account_id=acc, match_type='manual'),
            ['schedule_row__channel', 'schedule_row__month'], ['channel', 'month']),
        'period_sponsorships': _rows(PeriodSponsorship.objects.filter(account_id=acc),
                                     ['channel', 'month', 'start_date', 'end_date', 'planned_count', 'theme']),
        'transmission_reports': _rows(TransmissionReport.objects.filter(account_id=acc),
                                      ['channel', 'month', 'schedule_id', 'uploaded_at', 'row_count']),
        'schedules': _rows(Schedule.objects.filter(account_id=acc),
                           ['channel', 'month', 'version', 'is_superseded', 'is_locked']),
        'monitoring_uploaded_max': MonitoringData.objects.filter(account_id=acc)
        .aggregate(m=Max('uploaded_at'))['m'],
        'settings': {k: get_setting(k, '') for k in SETTING_KEYS},
        # scope parts
        # MonitoringData stores the clean channel name like LMRBRow: use the engine's filter
        'monitoring_data': sorted(MonitoringData.objects.filter(_lmrb_channel_q(ch), account_id=acc)
                                  .order_by().values_list('id', flat=True)),
        # Deliberately flag-agnostic: a count of the scope's LMRB rows, not a candidate query.
        'lmrb_count': lmrb_scope_qs(acc, ch, start, end).count() if start else 0,
        'match_results_run_max': MatchResult.objects.filter(account_id=acc, channel=ch, month=mo)
        .aggregate(m=Max('run_at'))['m'],
    }
    return to_jsonable(data)


def fingerprint_sha(fp: dict) -> str:
    return sha256_of(fp)


def is_current(fp: dict | None) -> bool:
    """False for a snapshot saved before 1.1 (no fingerprint) or in the 1.1 layout."""
    return bool(fp) and fp.get('version') == VERSION


def diff(old: dict, new: dict) -> dict:
    """Rows added, removed or changed, by id and field. Empty dict = no change."""
    old, new = old or {}, new or {}
    out = {}
    for sec in ROW_SECTIONS:
        a, b = old.get(sec, {}), new.get(sec, {})
        added = sorted(set(b) - set(a), key=_idkey)
        removed = sorted(set(a) - set(b), key=_idkey)
        changed = {}
        for k in sorted(set(a) & set(b), key=_idkey):
            fields = {f: [a[k].get(f), b[k].get(f)] for f in sorted(set(a[k]) | set(b[k]))
                      if a[k].get(f) != b[k].get(f)}
            if fields:
                changed[k] = fields
        if added or removed or changed:
            out[sec] = {'added': added, 'removed': removed, 'changed': changed}
    for sec in ID_SECTIONS:
        a, b = set(old.get(sec, [])), set(new.get(sec, []))
        if a != b:
            out[sec] = {'added': sorted(b - a), 'removed': sorted(a - b), 'changed': {}}
    for sec in SCALAR_SECTIONS:
        if old.get(sec) != new.get(sec):
            out[sec] = {'old': old.get(sec), 'new': new.get(sec)}
    sa, sb = old.get('settings', {}), new.get('settings', {})
    changed = {k: [sa.get(k), sb.get(k)] for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)}
    if changed:
        out['settings'] = {'added': [], 'removed': [], 'changed': changed}
    return out


# ── Scope relevance (Phase 1.2 item 3) ────────────────────────────────────────

def scope_context(scope) -> dict:
    """What the scope contains: brands of its active ScheduleRows, and the normalised
    themes of its LMRBRows and TCRows."""
    from .checks import lmrb_scope_qs
    from .scope import active_schedules, period
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    active = active_schedules(scope)
    start, end = period(scope, active)
    brands = {_norm(b) for b in ScheduleRow.objects.filter(schedule__in=active)
              .order_by().values_list('brand', flat=True).distinct()}
    lmrb = set()
    if start:
        # Deliberately flag-agnostic: every theme present in the scope, not a candidate query.
        lmrb = {_norm(t) for t in lmrb_scope_qs(acc, ch, start, end)
                .order_by().values_list('advt_theme', flat=True).distinct()}
    tc = {_norm(t) for t in TCRow.objects.filter(account_id=acc, channel=ch, tc_report__month=mo)
          .order_by().values_list('tc_theme', flat=True).distinct()}
    return {'channel': ch, 'month': mo, 'brands': brands - {''},
            'lmrb_themes': lmrb - {''}, 'tc_themes': tc - {''}}


def _theme_hits(value, themes: set) -> bool:
    """A mapping value (pipe-separated; '*' suffix = prefix match) hits any scope theme."""
    for part in str(value or '').split('|'):
        p = _norm(part)
        if not p:
            continue
        if p.endswith('*'):
            prefix = p[:-1].strip()
            if prefix and any(t.startswith(prefix) for t in themes):
                return True
        elif p in themes:
            return True
    return False


def _row_relevant(sec: str, row: dict | None, ctx: dict) -> bool:
    if not row:
        return False
    if sec == 'brand_mappings':
        return (_norm(row.get('brand')) in ctx['brands']
                or _theme_hits(row.get('theme'), ctx['lmrb_themes'])
                or _theme_hits(row.get('tc_theme'), ctx['tc_themes']))
    if sec == 'tc_lmrb_theme_maps':
        return _theme_hits(row.get('tc_theme'), ctx['tc_themes'])
    if sec in IN_SCOPE_BY_KEY:
        return row.get('channel') == ctx['channel'] and row.get('month') == ctx['month']
    return False


def relevant_diff(full: dict, old: dict, new: dict, ctx: dict) -> dict:
    """The part of `full` (from diff(old, new)) that touches this scope. A row counts if
    its old OR new version is relevant (a brand renamed away from this scope counts)."""
    out = {}
    for sec, d in full.items():
        if sec in INFO_ONLY:
            continue
        if sec in ALWAYS_RELEVANT:
            out[sec] = d
            continue
        a, b = (old or {}).get(sec, {}), (new or {}).get(sec, {})
        keep = {
            'added': [k for k in d['added'] if _row_relevant(sec, b.get(k), ctx)],
            'removed': [k for k in d['removed'] if _row_relevant(sec, a.get(k), ctx)],
            'changed': {k: v for k, v in d['changed'].items()
                        if _row_relevant(sec, a.get(k), ctx) or _row_relevant(sec, b.get(k), ctx)},
        }
        if keep['added'] or keep['removed'] or keep['changed']:
            out[sec] = keep
    return out


def _idkey(k):
    return (0, int(k)) if str(k).isdigit() else (1, str(k))
