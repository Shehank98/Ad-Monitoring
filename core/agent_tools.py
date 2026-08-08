"""
Shared, deterministic tool layer for the Ogilvy Nova agents.

These functions are the ONLY way the agents touch the database. They wrap the
real models/engines (see verification/engine.py, tc_engine.py) — they never
reimplement matching logic. Both the Investigation Agent ("Nova", core/
agent_chat.py) and the future background Reconciliation Agent call the same
functions here, so behaviour and guardrails stay identical across both.

Design rules baked in:
  * Never guess a brand match by time proximity alone — mapping is required
    evidence. These tools surface candidates and mapping state; they do not
    auto-link on similarity.
  * Honour ALL FOUR LMRBRow lock flags (is_matched, is_sponsorship_matched,
    is_manual_matched, is_tc_lmrb_matched) so a row is never double-claimed.
  * propose_manual_match is Tier-3 (always human): it refuses to act unless
    confirmed_by_user is True — the live chat confirmation IS that human step.
"""
from __future__ import annotations

from datetime import datetime
from difflib import SequenceMatcher

from core.models import (
    Account, BrandMapping, LMRBRow, ManualMatch, ScheduleRow, TCRow,
)
from verification.engine import _lmrb_channel_q


# ── time helpers ────────────────────────────────────────────────────────────

def _parse_hms(value) -> datetime | None:
    """Parse 'HH:MM:SS' or 'HH:MM' (or a time object's str) → datetime, or None."""
    if value in (None, ''):
        return None
    s = str(value).strip()
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ── Nova investigation tools ────────────────────────────────────────────────

def get_schedule_row_context(schedule_row_id: int) -> dict:
    """Return the planned details of a scheduled spot the user is asking about."""
    try:
        row = ScheduleRow.objects.select_related('schedule').get(id=schedule_row_id)
    except ScheduleRow.DoesNotExist:
        return {'error': f'schedule_row_id {schedule_row_id} not found'}
    return {
        'schedule_row_id': row.id,
        'brand': row.brand,
        'channel': row.channel,
        'month': row.month,
        'date': str(row.date),
        'planned_start': str(row.start_time),
        'planned_end': str(row.end_time),
        'duration': row.duration,
        'ad_type': row.ad_type,
        'account_id': row.account_id,
        'is_matched': row.is_matched,
        'is_manual_matched': row.is_manual_matched,
    }


def search_lmrb_candidates(channel: str, date: str, planned_time: str,
                           window_minutes: int = 60) -> dict:
    """Search LMRB for possible airings near a planned time on the same
    channel/date, IGNORING exact programme-name matching (programme names differ
    between sources — that mismatch is usually the real bug). Channel matching is
    prefix-tolerant ('TV - X' vs 'X'). Sorted by closeness to the planned time.
    """
    planned = _parse_hms(planned_time)
    qs = LMRBRow.objects.filter(_lmrb_channel_q(channel), date=date).order_by('advt_time')
    results = []
    for c in qs:
        c_time = _parse_hms(c.advt_time)
        diff_min = (abs((c_time - planned).total_seconds()) / 60
                    if (planned and c_time) else None)
        if diff_min is not None and diff_min > window_minutes:
            continue
        results.append({
            'lmrb_row_id': c.id,
            'theme': c.advt_theme,
            'aired_time': str(c.advt_time),
            'duration': c.duration,
            'minutes_from_planned': round(diff_min, 1) if diff_min is not None else None,
            'source': c.source,
            'product': c.product or '',
            # A row already claimed by ANY engine cannot be re-linked.
            'already_locked': bool(
                c.is_matched or c.is_sponsorship_matched
                or c.is_manual_matched or c.is_tc_lmrb_matched
            ),
        })
    results.sort(key=lambda r: (r['minutes_from_planned'] is None,
                                r['minutes_from_planned'] or 0))
    return {'candidates': results[:10], 'total_found': len(results)}


def check_brand_mapping(account_id: int, brand: str) -> dict:
    """Check whether a brand has an active BrandMapping and what it maps to.
    A brand with no mapping (or the wrong theme mapped) is very often the real
    cause of a 'Not Aired' — not a genuine miss."""
    mappings = BrandMapping.objects.filter(account_id=account_id, brand=brand)
    return {
        'is_mapped': mappings.exists(),
        'mapped_lmrb_themes': [m.theme for m in mappings if m.theme],
        'mapped_tc_themes': [m.tc_theme for m in mappings if m.tc_theme],
        'mapped_maponline_themes': [m.maponline_theme for m in mappings if m.maponline_theme],
    }


def propose_manual_match(schedule_row_id: int, lmrb_row_id: int,
                         confirmed_by_user: bool, user=None) -> dict:
    """TIER 3 — always human. Create a schedule_lmrb ManualMatch, but ONLY when
    the live user has explicitly confirmed this candidate. The agent never
    self-authorises a lock. Honours the row-level locks on both sides."""
    if not confirmed_by_user:
        return {'status': 'not_created', 'reason': 'awaiting explicit user confirmation'}
    try:
        sr = ScheduleRow.objects.select_related('schedule').get(id=schedule_row_id)
    except ScheduleRow.DoesNotExist:
        return {'status': 'error', 'reason': f'schedule_row {schedule_row_id} not found'}
    try:
        lr = LMRBRow.objects.get(id=lmrb_row_id)
    except LMRBRow.DoesNotExist:
        return {'status': 'error', 'reason': f'lmrb_row {lmrb_row_id} not found'}

    if ManualMatch.objects.filter(schedule_row=sr).exists():
        return {'status': 'already_existed', 'reason': 'schedule row already manually matched'}
    if ManualMatch.objects.filter(lmrb_row=lr).exists():
        return {'status': 'already_existed', 'reason': 'LMRB row already manually matched'}
    if lr.is_sponsorship_matched or lr.is_tc_lmrb_matched:
        return {'status': 'not_created', 'reason': 'LMRB row is locked by another engine'}

    mm = ManualMatch.objects.create(
        account_id=sr.account_id, channel=sr.channel, month=sr.month,
        match_mode='schedule_lmrb', schedule_row=sr, lmrb_row=lr,
        matched_by=user if (user and getattr(user, 'is_authenticated', False)) else None,
    )
    ScheduleRow.objects.filter(id=sr.id).update(is_manual_matched=True)
    LMRBRow.objects.filter(id=lr.id).update(is_manual_matched=True)
    return {'status': 'created', 'match_id': mm.id}


# ── Upload & Mapping Guardian (Tier 1 — advisory, never destructive) ─────────

def validate_upload_selection(step: str, account_id, brand_hint_from_filename: str = None) -> dict:
    """Runs before a Schedule/LMRB/TC upload commits.

    Hard-blocks when no Account was selected (the #1 mistake — the file would
    have nowhere to reconcile). Soft-warns when the filename suggests a
    different brand than the one selected (wrong-tab / copy-paste mistake).
    """
    if not account_id:
        return {
            'block': True,
            'warnings': [],
            'message': (f'No brand (Account) is selected for this {step} upload — '
                        f'pick the brand first, otherwise this file has nowhere to '
                        f'go and will not reconcile against anything.'),
        }
    warnings = []
    if brand_hint_from_filename:
        try:
            account = Account.objects.get(id=account_id)
            sim = SequenceMatcher(None, brand_hint_from_filename.lower(),
                                  account.name.lower()).ratio()
            if sim < 0.4:
                warnings.append(
                    f"This file's name suggests '{brand_hint_from_filename}', but "
                    f"you've selected '{account.name}' — double-check you picked the "
                    f"right brand before saving."
                )
        except Account.DoesNotExist:
            warnings.append('The selected brand (Account) could not be found.')
    return {'block': False, 'warnings': warnings, 'message': ''}


def audit_brand_mapping(account_id: int, brand: str) -> dict:
    """Runs right after a Quick Map save. Returns plain-language warnings about
    the common mapping mistakes — never blocks, just flags:
      1. a Product filter set without a Duration (the silent-drop footgun);
      2. a theme already mapped to a different brand in this account;
      3. an unmapped LMRB theme that looks like it belongs to this brand.
    """
    warnings = []
    mappings = list(BrandMapping.objects.filter(account_id=account_id, brand=brand))

    # 1. product filter set without a duration — forces exact-product match
    for m in mappings:
        if m.product and not m.duration:
            warnings.append(
                f"Your mapping for '{brand}' → '{m.theme}' has a Product filter set "
                f"without a Duration — this forces an exact-product match and can "
                f"silently drop real airings. Clear Product unless you're deliberately "
                f"narrowing it."
            )

    # 2. theme already claimed by a different brand in this account
    theme_names = [m.theme for m in mappings if m.theme]
    if theme_names:
        for c in BrandMapping.objects.filter(
            account_id=account_id, theme__in=theme_names
        ).exclude(brand=brand):
            warnings.append(
                f"Theme '{c.theme}' is already mapped to a different brand "
                f"('{c.brand}') in this account — check this isn't a mix-up between "
                f"two brands."
            )

    # 3. unmapped LMRB theme that looks like it belongs to this brand
    mapped = {t.lower().strip() for t in theme_names}
    seen = set()
    for theme in (LMRBRow.objects.filter(account_id=account_id)
                  .exclude(advt_theme='')
                  .values_list('advt_theme', flat=True).distinct()):
        key = theme.lower().strip()
        if key in mapped or key in seen:
            continue
        seen.add(key)
        if SequenceMatcher(None, key, brand.lower()).ratio() > 0.6:
            warnings.append(
                f"There's an unmapped LMRB theme '{theme}' that looks like it might "
                f"belong to '{brand}' — did you forget to include it?"
            )

    return {'warnings': warnings, 'is_clean': len(warnings) == 0}


# ── Root-Cause Finder (Tier 2 — evidence only; a human still edits mappings) ──

def diagnose_unmatched_tc_spot(tc_row_id: int) -> dict:
    """Given a TC spot that failed to reconcile, search its RAW tc_theme string
    against every BrandMapping in the WHOLE system (not just this account) to
    find where the same/similar theme is already recognised, and cross-check
    LMRB for the same slot. Surfaces the specific 'missing mapping' or 'wrong
    brand' fix. Never edits a mapping — that stays human (Tier 2/3)."""
    try:
        tc = TCRow.objects.select_related('account').get(id=tc_row_id)
    except TCRow.DoesNotExist:
        return {'error': f'tc_row_id {tc_row_id} not found'}

    raw = (tc.tc_theme or '').strip()
    raw_l = raw.lower()

    exact_elsewhere, fuzzy = [], []
    for m in (BrandMapping.objects.exclude(tc_theme='').exclude(tc_theme__isnull=True)
              .select_related('account')):
        for variant in m.tc_theme.split('|'):
            v = variant.strip()
            if not v:
                continue
            vl = v.lower()
            # wildcard-aware exact: 'X*' matches themes starting with 'X'
            is_exact = (raw_l.startswith(vl[:-1]) if vl.endswith('*') else raw_l == vl)
            if is_exact:
                exact_elsewhere.append({
                    'brand': m.brand, 'account_id': m.account_id,
                    'account_name': getattr(m.account, 'name', None),
                    'same_account': m.account_id == tc.account_id,
                    'matched_variant': v,
                })
            elif raw_l:
                score = SequenceMatcher(None, raw_l, vl.rstrip('*')).ratio()
                if score > 0.55:
                    fuzzy.append({
                        'brand': m.brand, 'account_id': m.account_id,
                        'account_name': getattr(m.account, 'name', None),
                        'matched_variant': v, 'similarity': round(score, 2),
                        'same_account': m.account_id == tc.account_id,
                    })
    fuzzy.sort(key=lambda c: -c['similarity'])

    # LMRB echo: same channel/date within 5 min of the TC aired time — confirms
    # the ad is real and shows which brand its LMRB theme already maps to.
    tc_time = _parse_hms(tc.aired_time)
    lmrb_confirmation = []
    if tc_time is not None:
        for r in LMRBRow.objects.filter(_lmrb_channel_q(tc.channel), date=tc.date):
            rt = _parse_hms(r.advt_time)
            if rt is None or abs((rt - tc_time).total_seconds()) > 300:
                continue
            bm = BrandMapping.objects.filter(
                account_id=tc.account_id, theme__iexact=(r.advt_theme or '')).first()
            lmrb_confirmation.append({
                'lmrb_theme': r.advt_theme,
                'aired_time': str(r.advt_time),
                'mapped_to_brand': bm.brand if bm else None,
            })

    return {
        'raw_tc_theme': raw,
        'channel': tc.channel,
        'date': str(tc.date),
        'duration': tc.duration,
        'currently_assigned_account': getattr(tc.account, 'name', None),
        'currently_assigned_account_id': tc.account_id,
        'exact_match_elsewhere': exact_elsewhere,
        'fuzzy_candidates': fuzzy[:5],
        'lmrb_confirmation': lmrb_confirmation,
    }


# ── Scope-level tool: enumerate the unmatched spots for a Summary Sheet ───────

def list_unmatched_spots(account_id: int, channel: str, month: str,
                         schedule_id=None, limit: int = 100) -> dict:
    """List the deviations (Missed / Extra) for a (account, channel, month) scope
    EXACTLY as the Summary Sheet shows them.

    This reads verification.tc_engine.build_summary_data — the SAME source the
    Summary Sheet and its 'Transmission Report Details' deviation table use — so
    Nova's counts always agree with what the user sees. (The previous version read
    MatchResult, which is only written by the commercial Schedule↔LMRB engine, so
    sponsorship/tag deviations showed as 0 while the summary showed them.)

    For each deviating brand it also lists the underlying unmatched ScheduleRow
    ids, so Nova can drill into individual spots with get_schedule_row_context /
    search_lmrb_candidates.
    """
    from verification.tc_engine import build_summary_data
    sid = int(schedule_id) if schedule_id else None
    data = build_summary_data(account_id, channel, month, schedule_id=sid)

    deviations = []
    total_missed = total_extra = 0

    def _add(row, kind, programme=''):
        nonlocal total_missed, total_extra
        missed = row.get('missed', 0) or 0
        extra = row.get('extra', 0) or 0
        if not (missed or extra):
            return
        total_missed += missed
        total_extra += extra
        deviations.append({
            'brand': row.get('product') or programme or '',
            'duration': row.get('dur'),
            'planned': row.get('planned', 0) or 0,
            'aired': row.get('aired', 0) or 0,
            'third_party': row.get('third_party', 0) or 0,
            'missed': missed,
            'extra': extra,
            'kind': kind,
            'programme': programme,
        })

    for row in (data.get('commercial') or []):
        _add(row, 'commercial')
    for section in (data.get('sponsorship') or []):
        for row in (section.get('rows') or []):
            _add(row, 'sponsorship', section.get('programme', ''))

    # Attach the actual unmatched ScheduleRow ids per deviating brand for drill-down.
    for dev in deviations:
        srq = ScheduleRow.objects.filter(
            account_id=account_id, channel__iexact=channel, month=month,
            brand=dev['brand'], is_matched=False, is_manual_matched=False,
        )
        if sid:
            srq = srq.filter(schedule_id=sid)
        if dev['duration'] is not None:
            srq = srq.filter(duration=dev['duration'])
        dev['unmatched_schedule_row_ids'] = list(srq.values_list('id', flat=True)[:limit])

    return {
        'scope': {'account_id': account_id, 'channel': channel, 'month': month},
        'total_missed': total_missed,     # matches the summary's "Not Aired" total
        'total_extra': total_extra,
        'deviations': deviations,
    }


# ── Batch investigation + TC-side action (so Nova needs no IDs from the user) ──

def investigate_all_unmatched(account_id: int, channel: str, month: str,
                              schedule_id=None, max_spots: int = 25) -> dict:
    """Investigate EVERY unmatched spot in a scope in one call — both the missed
    schedule spots AND the unmatched TC spots — so Nova can work from the Summary
    Sheet without the user supplying any schedule_id / tc_row_id.

    For each missed schedule spot: the closest LMRB airing (programme-agnostic,
    ±120 min) + whether the brand is mapped.
    For each unmatched TC spot (not LMRB-confirmed): the closest LMRB airing +
    the whole-system root-cause (diagnose_unmatched_tc_spot).

    Returns everything Nova needs to (a) explain each case and (b) call
    propose_manual_match / propose_tc_lmrb_match to actually link them on the
    user's confirmation.
    """
    findings_schedule, findings_tc = [], []

    # 1) Missed schedule spots (drill-down ids come straight from the summary).
    scope = list_unmatched_spots(account_id, channel, month, schedule_id=schedule_id)
    seen = 0
    for dev in scope['deviations']:
        for srid in dev.get('unmatched_schedule_row_ids', []):
            if seen >= max_spots:
                break
            ctx = get_schedule_row_context(srid)
            if 'error' in ctx:
                continue
            cands = search_lmrb_candidates(ctx['channel'], ctx['date'],
                                           ctx['planned_start'], window_minutes=120)
            best = cands['candidates'][0] if cands['candidates'] else None
            mapping = check_brand_mapping(account_id, ctx['brand'])
            findings_schedule.append({
                'schedule_row_id': srid,
                'brand': ctx['brand'], 'date': ctx['date'],
                'planned_start': ctx['planned_start'], 'duration': ctx['duration'],
                'is_mapped': mapping['is_mapped'],
                'candidate_count': cands['total_found'],
                'best_candidate': best,   # has lmrb_row_id, theme, gap, already_locked
            })
            seen += 1

    # 2) Unmatched TC spots for the scope (schedule-matched or extra, but not
    #    LMRB-confirmed) — the ones the Summary counts against Aired.
    tcq = (TCRow.objects.filter(account_id=account_id, channel__iexact=channel,
                                tc_report__month=month, is_lmrb_confirmed=False)
           .order_by('date', 'aired_time'))
    if schedule_id:
        tcq = tcq.filter(tc_report__schedule_id=schedule_id)
    for tc in tcq[:max_spots]:
        cands = search_lmrb_candidates(tc.channel, str(tc.date), tc.aired_time,
                                       window_minutes=120)
        best = cands['candidates'][0] if cands['candidates'] else None
        diag = diagnose_unmatched_tc_spot(tc.id)
        findings_tc.append({
            'tc_row_id': tc.id,
            'tc_theme': tc.tc_theme, 'date': str(tc.date),
            'aired_time': tc.aired_time, 'duration': tc.duration,
            'is_schedule_matched': tc.is_schedule_matched,
            'candidate_count': cands['total_found'],
            'best_candidate': best,
            'root_cause': {
                'exact_match_elsewhere': diag.get('exact_match_elsewhere', []),
                'fuzzy_candidates': diag.get('fuzzy_candidates', []),
                'lmrb_confirmation': diag.get('lmrb_confirmation', []),
            },
        })

    return {
        'scope': {'account_id': account_id, 'channel': channel, 'month': month},
        'summary_totals': {'missed': scope['total_missed'], 'extra': scope['total_extra']},
        'missed_schedule_spots': findings_schedule,
        'unmatched_tc_spots': findings_tc,
        'note': ('best_candidate.already_locked=True means that LMRB row is taken '
                 'and cannot be linked. Only propose links the user confirms.'),
    }


def propose_tc_lmrb_match(tc_row_id: int, lmrb_row_id: int,
                          confirmed_by_user: bool, user=None) -> dict:
    """TIER 3 — always human. Link a TC spot to an LMRB row (match_mode='tc_lmrb')
    once the live user confirms — for TC spots that have no schedule row. Sets the
    TC row LMRB-confirmed and locks the LMRB row. Never acts unprompted."""
    if not confirmed_by_user:
        return {'status': 'not_created', 'reason': 'awaiting explicit user confirmation'}
    try:
        tc = TCRow.objects.select_related('tc_report').get(id=tc_row_id)
    except TCRow.DoesNotExist:
        return {'status': 'error', 'reason': f'tc_row {tc_row_id} not found'}
    try:
        lr = LMRBRow.objects.get(id=lmrb_row_id)
    except LMRBRow.DoesNotExist:
        return {'status': 'error', 'reason': f'lmrb_row {lmrb_row_id} not found'}

    if ManualMatch.objects.filter(tc_row=tc).exists():
        return {'status': 'already_existed', 'reason': 'TC row already manually matched'}
    if ManualMatch.objects.filter(lmrb_row=lr).exists():
        return {'status': 'already_existed', 'reason': 'LMRB row already manually matched'}
    if lr.is_sponsorship_matched or lr.is_tc_lmrb_matched:
        return {'status': 'not_created', 'reason': 'LMRB row is locked by another engine'}

    mm = ManualMatch.objects.create(
        account_id=tc.account_id, channel=tc.channel,
        month=tc.tc_report.month if tc.tc_report else '',
        match_mode='tc_lmrb', tc_row=tc, lmrb_row=lr,
        matched_by=user if (user and getattr(user, 'is_authenticated', False)) else None,
    )
    TCRow.objects.filter(id=tc.id).update(is_lmrb_confirmed=True, matched_lmrb_id=lr.id)
    LMRBRow.objects.filter(id=lr.id).update(is_manual_matched=True)
    return {'status': 'created', 'match_id': mm.id}


# ── Nova v2: system-wide lookup + navigation/report ACTIONS ──────────────────
# Navigation/download are read-only and reversible (Tier 1): the tool returns an
# {'action': 'navigate'|'download', 'url': ...} that the browser executes. URLs
# point at the REAL existing routes (see core/urls.py) — no new endpoints.

from urllib.parse import urlencode  # noqa: E402


def lookup_schedule(schedule_number_or_id) -> dict:
    """System-wide search for a schedule by its schedule_number or id (NOT scoped
    to the current page/account). Returns the scope(s) so Nova can open them."""
    from core.models import Schedule
    val = str(schedule_number_or_id).strip()
    from django.db.models import Q as _Q
    q = _Q(schedule_number__iexact=val)
    if val.isdigit():
        q |= _Q(id=int(val))
    schedules = list(Schedule.objects.select_related('account').filter(q).order_by('-version')[:10])
    if not schedules:
        return {'found': False}
    matches = [{
        'schedule_id': s.id, 'schedule_number': s.schedule_number,
        'account': s.account.name if s.account_id else '', 'account_id': s.account_id,
        'channel': s.channel, 'month': s.month, 'row_count': s.row_count,
    } for s in schedules]
    out = {'found': True, 'count': len(matches), 'matches': matches}
    out.update(matches[0])  # convenience: top match fields at the root
    return out


def lookup_by_brand(brand: str, month: str = None) -> dict:
    """Find the distinct scopes (account, channel, month) that carry a brand,
    anywhere in the system. Optionally filter by month."""
    qs = ScheduleRow.objects.select_related('schedule', 'schedule__account').filter(
        brand__icontains=(brand or '').strip())
    if month:
        qs = qs.filter(month=month)
    seen, out = set(), []
    for r in qs.order_by('brand')[:400]:
        key = (r.account_id, r.channel, r.month, r.brand)
        if key in seen:
            continue
        seen.add(key)
        out.append({'account': r.schedule.account.name if r.schedule and r.schedule.account_id else '',
                    'account_id': r.account_id, 'channel': r.channel,
                    'month': r.month, 'brand': r.brand})
        if len(out) >= 10:
            break
    return {'matches': out}


def open_summary_sheet(account_id: int, channel: str, month: str, schedule_id=None) -> dict:
    """Navigate to the Reconciliation / Summary Sheet for a scope."""
    params = {'account_id': account_id, 'channel': channel, 'month': month}
    if schedule_id:
        params['schedule_id'] = schedule_id
    return {'action': 'navigate', 'url': '/dashboard/summary/?' + urlencode(params)}


def open_schedule_detail(schedule_row_id: int) -> dict:
    """Navigate to the three-way TC detail for the scope a spot belongs to."""
    try:
        row = ScheduleRow.objects.get(id=schedule_row_id)
    except ScheduleRow.DoesNotExist:
        return {'error': f'schedule_row_id {schedule_row_id} not found'}
    return {'action': 'navigate',
            'url': '/dashboard/tc/detail/?' + urlencode(
                {'account_id': row.account_id, 'channel': row.channel, 'month': row.month})}


def open_mapping_page(account_id: int, brand: str = '') -> dict:
    """Navigate to the Quick Map (brand mapping) screen for an account."""
    return {'action': 'navigate',
            'url': '/dashboard/brand-mappings/quick/?' + urlencode({'account': account_id})}


def generate_summary_report(account_id: int, channel: str, month: str,
                            format: str = 'xlsx', schedule_id=None) -> dict:
    """Return a download action for the Summary report. Reuses the existing
    server-side export routes (summary_excel / summary_pdf) — no new generator."""
    params = {'account_id': account_id, 'channel': channel, 'month': month}
    if schedule_id:
        params['schedule_id'] = schedule_id
    is_pdf = str(format).lower() in ('pdf', 'p')
    base = '/dashboard/summary/pdf/' if is_pdf else '/dashboard/summary/excel/'
    return {'action': 'download', 'url': base + '?' + urlencode(params),
            'format': 'pdf' if is_pdf else 'xlsx'}


def propose_mapping_fix(account_id: int, brand: str, tc_theme: str, reasoning: str) -> dict:
    """TIER 3 — names a BrandMapping fix, does NOT apply it. Nova presents this and
    the operator applies it on the mapping screen (open_mapping_page)."""
    return {'action': 'confirm_required',
            'fix': {'account_id': account_id, 'brand': brand, 'tc_theme': tc_theme},
            'reasoning': reasoning}
