"""
External-change fingerprint (Phase 1.1, owner decision 3). Read-only.

A canonical snapshot of everything people change through the core UI for one scope's
account, so V5 can tell "the numbers moved because someone edited a mapping / uploaded /
changed a setting" from "the numbers moved and nothing explains it".

Contents (field names checked against core/models.py; none missing):
  brand_mappings            BrandMapping rows of the account: every field except account
  tc_lmrb_theme_maps        TcLmrbThemeMap rows: tc_theme, tc_duration, lmrb_theme, lmrb_duration
  manual_matches            ManualMatch ids (account)
  manual_sponsorship        SponsorshipLmrbAssignment ids with match_type='manual' (account)
  period_sponsorships       PeriodSponsorship: start_date, end_date, planned_count, theme
  transmission_reports      TransmissionReport: schedule_id, uploaded_at, row_count
  schedules                 Schedule: version, is_superseded, is_locked
  monitoring_uploaded_max   max MonitoringData.uploaded_at (account)
  match_results_run_max     max MatchResult.run_at for the scope (core engine runs by people
                            or the upload thread)
  settings                  via get_setting: tc_lmrb_time_tolerance, every tc_extra_* and
                            lmrb_extra_* alias, lmrb_sponsorship_keywords
"""
from __future__ import annotations

from django.db.models import Max

from core.models import (
    BrandMapping, ManualMatch, MatchResult, MonitoringData, PeriodSponsorship, Schedule,
    SponsorshipLmrbAssignment, TcLmrbThemeMap, TransmissionReport, get_setting,
)

from .canonical import sha256_of, to_jsonable

ALIAS_KEYS = [
    'tc_extra_theme_aliases', 'tc_extra_time_aliases', 'tc_extra_date_aliases',
    'tc_extra_duration_aliases', 'tc_extra_programme_aliases',
    'lmrb_extra_theme_aliases', 'lmrb_extra_time_aliases', 'lmrb_extra_duration_aliases',
    'lmrb_extra_date_aliases',
]
SETTING_KEYS = ['tc_lmrb_time_tolerance', *ALIAS_KEYS, 'lmrb_sponsorship_keywords']

ROW_SECTIONS = ('brand_mappings', 'tc_lmrb_theme_maps', 'period_sponsorships',
                'transmission_reports', 'schedules')
ID_SECTIONS = ('manual_matches', 'manual_sponsorship')
SCALAR_SECTIONS = ('monitoring_uploaded_max', 'match_results_run_max', 'settings')


def _rows(qs, fields) -> dict:
    return {str(r['id']): {f: r[f] for f in fields} for r in qs.order_by().values('id', *fields)}


def fingerprint(scope) -> dict:
    acc, ch, mo = scope.account_id, scope.channel, scope.month
    bm_fields = ['product', 'brand', 'theme', 'tc_theme', 'maponline_theme', 'duration']
    data = {
        'brand_mappings': _rows(BrandMapping.objects.filter(account_id=acc), bm_fields),
        'tc_lmrb_theme_maps': _rows(TcLmrbThemeMap.objects.filter(account_id=acc),
                                    ['tc_theme', 'tc_duration', 'lmrb_theme', 'lmrb_duration']),
        'manual_matches': sorted(ManualMatch.objects.filter(account_id=acc).order_by()
                                 .values_list('id', flat=True)),
        'manual_sponsorship': sorted(SponsorshipLmrbAssignment.objects.filter(
            account_id=acc, match_type='manual').order_by().values_list('id', flat=True)),
        'period_sponsorships': _rows(PeriodSponsorship.objects.filter(account_id=acc),
                                     ['start_date', 'end_date', 'planned_count', 'theme']),
        'transmission_reports': _rows(TransmissionReport.objects.filter(account_id=acc),
                                      ['schedule_id', 'uploaded_at', 'row_count']),
        'schedules': _rows(Schedule.objects.filter(account_id=acc),
                           ['version', 'is_superseded', 'is_locked']),
        'monitoring_uploaded_max': MonitoringData.objects.filter(account_id=acc)
        .aggregate(m=Max('uploaded_at'))['m'],
        'match_results_run_max': MatchResult.objects.filter(account_id=acc, channel=ch, month=mo)
        .aggregate(m=Max('run_at'))['m'],
        'settings': {k: get_setting(k, '') for k in SETTING_KEYS},
    }
    return to_jsonable(data)


def fingerprint_sha(fp: dict) -> str:
    return sha256_of(fp)


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
    for sec in ('monitoring_uploaded_max', 'match_results_run_max'):
        if old.get(sec) != new.get(sec):
            out[sec] = {'old': old.get(sec), 'new': new.get(sec)}
    sa, sb = old.get('settings', {}), new.get('settings', {})
    changed = {k: [sa.get(k), sb.get(k)] for k in sorted(set(sa) | set(sb)) if sa.get(k) != sb.get(k)}
    if changed:
        out['settings'] = {'added': [], 'removed': [], 'changed': changed}
    return out


def _idkey(k):
    return (0, int(k)) if str(k).isdigit() else (1, str(k))
