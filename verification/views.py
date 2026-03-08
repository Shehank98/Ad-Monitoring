"""
Verification views.

Run modes (used by run_verification):
  smart  (default) — skip already-matched schedule rows; pre-lock consumed LMRB rows.
  reset             — delete all MatchResults for the scope, then run from scratch.

Exports are DB-based: they read persisted MatchResult records and never re-run
the matching engine.  This makes downloads instant regardless of file size.
"""
import io

import pandas as pd

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from django.db.models import Min, Max

from core.models import Account, LMRBRow, MatchResult, MonitoringData, Schedule, ScheduleRow
from .engine import run_scope, active_schedule_ids


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _is_admin(user):
    return user.role in ('super_admin', 'admin')


def _account_access(user, account_id):
    if _is_admin(user):
        return True
    return user.accounts.filter(id=account_id).exists()


# ── Export helpers ─────────────────────────────────────────────────────────────

def _qs_to_df(qs):
    """Convert a MatchResult queryset to a DataFrame with human-readable columns."""
    STATUS_LABELS = dict(MatchResult.STATUS_CHOICES)
    rows = []
    for mr in qs:
        rows.append({
            'Channel':       mr.channel,
            'Month':         mr.month,
            'Brand':         mr.brand,
            'Theme':         mr.theme,
            'Duration (s)':  mr.duration,
            'Programme':     mr.programme,
            'Planned Date':  mr.scheduled_date,
            'Planned Start': mr.planned_start,
            'Planned End':   mr.planned_end,
            'Aired Date':    mr.aired_date,
            'Aired Time':    mr.air_time,
            'Source':        mr.source,
            'Status':        STATUS_LABELS.get(mr.status, mr.status),
        })
    if not rows:
        return pd.DataFrame(columns=[
            'Channel', 'Month', 'Brand', 'Theme', 'Duration (s)', 'Programme',
            'Planned Date', 'Planned Start', 'Planned End',
            'Aired Date', 'Aired Time', 'Source', 'Status',
        ])
    return pd.DataFrame(rows)


def _summary_df(full_df, group_col):
    """Build a summary DataFrame grouped by group_col."""
    if full_df.empty or group_col not in full_df.columns:
        return pd.DataFrame(columns=[
            group_col, 'Total Scheduled', 'Matched', 'Programme Mismatch',
            'Late Telecast', 'Not Aired', 'No Mapping', 'Compliance %',
        ])
    rows = []
    for val, grp in full_df.groupby(group_col):
        total      = len(grp)
        n_matched  = (grp['Status'] == 'Matched').sum()
        rows.append({
            group_col:             val,
            'Total Scheduled':     total,
            'Matched':             n_matched,
            'Programme Mismatch':  (grp['Status'] == 'Programme Mismatch').sum(),
            'Late Telecast':       (grp['Status'] == 'Late Telecast').sum(),
            'Not Aired':           (grp['Status'] == 'Not Aired').sum(),
            'No Mapping':          (grp['Status'] == 'No Brand Mapping').sum(),
            'Compliance %':        round(n_matched / total * 100, 1) if total else 0,
        })
    return pd.DataFrame(rows)


def _build_excel_response(qs, filename_base):
    """
    Build a multi-sheet Excel HttpResponse from a MatchResult queryset.

    Sheets: Full Report, Matched, Not Aired, Programme Mismatch, Late Telecast,
            Channel Summary, Brand Summary.
    """
    full_df = _qs_to_df(qs)

    def _sheet(status_label):
        if full_df.empty:
            return pd.DataFrame()
        return full_df[full_df['Status'] == status_label].copy()

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        full_df.to_excel(writer, sheet_name='Full Report', index=False)
        _sheet('Matched').to_excel(writer, sheet_name='Matched', index=False)
        _sheet('Not Aired').to_excel(writer, sheet_name='Not Aired', index=False)
        _sheet('Programme Mismatch').to_excel(writer, sheet_name='Programme Mismatch', index=False)
        _sheet('Late Telecast').to_excel(writer, sheet_name='Late Telecast', index=False)
        _summary_df(full_df, 'Channel').to_excel(writer, sheet_name='Channel Summary', index=False)
        _summary_df(full_df, 'Brand').to_excel(writer, sheet_name='Brand Summary', index=False)

    output.seek(0)
    fname = f'{filename_base}.xlsx'.replace(' ', '_')
    resp  = HttpResponse(
        output.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


# ── Internal df → JSON helper ──────────────────────────────────────────────────

def _df_to_list(df):
    if df is None or df.empty:
        return []
    df = df.copy()
    for col in ['_lmrb_fp', '_sch_key']:
        if col in df.columns:
            df.drop(columns=[col], inplace=True)
    for col in df.select_dtypes(include=['datetime64[ns]', 'datetime64[ns, UTC]']).columns:
        df[col] = df[col].dt.strftime('%Y-%m-%d')
    return df.fillna('').to_dict('records')


# ── Verify Ads summary helpers ──────────────────────────────────────────────────

def _status_dot(account_id, channel, month):
    """Return status indicator: 'none'|'ok'|'missed'|'error'."""
    qs = MatchResult.objects.filter(account_id=account_id, channel=channel, month=month)
    if not qs.exists():
        return 'none'
    counts = {s: 0 for s, _ in MatchResult.STATUS_CHOICES}
    for r in qs.values('status'):
        counts[r['status']] = counts.get(r['status'], 0) + 1
    if counts.get('not_aired', 0) > 0 or counts.get('programme_mismatch', 0) > 0:
        return 'missed'
    return 'ok'


def _build_campaign_rows(user):
    """Build one row per (account, channel, month) campaign that has a schedule."""
    if _is_admin(user):
        accounts_qs = Account.objects.all()
    else:
        accounts_qs = user.accounts.all()

    account_ids = list(accounts_qs.values_list('id', flat=True))

    # All distinct campaigns (account × channel × month) from Schedule
    combos = (
        Schedule.objects
        .filter(account_id__in=account_ids)
        .values('account_id', 'account__name', 'channel', 'month')
        .annotate(
            sch_start=Min('start_date'),
            sch_end=Max('end_date'),
            total_rows=Max('row_count'),  # sum across versions done below
        )
        .order_by('-month', 'channel')
    )

    rows = []
    for c in combos:
        a_id    = c['account_id']
        channel = c['channel']
        month   = c['month']

        # --- Schedule row count — COMMERCIAL BENEFITS only, active schedules only ---
        # Use the same active-schedule logic as the engine (highest version per
        # schedule_number) so duplicate uploads don't inflate the planned count.
        active_ids = active_schedule_ids(a_id, channel, month)
        planned = ScheduleRow.objects.filter(
            schedule_id__in=active_ids,
            ad_type='COMMERCIAL BENEFITS',
        ).count()

        # --- Schedule date range ---
        sch_range = Schedule.objects.filter(
            account_id=a_id, channel=channel, month=month
        ).aggregate(s=Min('start_date'), e=Max('end_date'))
        sch_start = sch_range['s']
        sch_end   = sch_range['e']

        # --- LMRB date range ---
        lmrb_range = LMRBRow.objects.filter(
            account_id=a_id, channel__iexact=channel
        ).aggregate(s=Min('date'), e=Max('date'))
        lmrb_start = lmrb_range['s']
        lmrb_end   = lmrb_range['e']

        # Amber flag: LMRB doesn't fully cover schedule period
        lmrb_amber = False
        if sch_start and sch_end and lmrb_start and lmrb_end:
            lmrb_amber = (lmrb_start > sch_start) or (lmrb_end < sch_end)

        # --- Verification results ---
        mr_qs = MatchResult.objects.filter(account_id=a_id, channel=channel, month=month)
        n_matched = mr_qs.filter(status='matched').count()
        n_missed  = mr_qs.filter(status='not_aired').count()
        last_run  = mr_qs.order_by('-run_at').values_list('run_at', flat=True).first()
        dot       = _status_dot(a_id, channel, month)

        # --- Schedule versions ---
        versions = list(
            Schedule.objects.filter(account_id=a_id, channel=channel, month=month)
            .order_by('version')
            .values('id', 'version', 'schedule_number', 'row_count',
                    'start_date', 'end_date', 'original_filename', 'is_superseded')
        )

        rows.append({
            'account_id':   a_id,
            'account_name': c['account__name'],
            'channel':      channel,
            'month':        month,
            'planned':      planned,
            'aired':        n_matched,
            'missed':       n_missed,
            'sch_start':    sch_start,
            'sch_end':      sch_end,
            'lmrb_start':   lmrb_start,
            'lmrb_end':     lmrb_end,
            'lmrb_amber':   lmrb_amber,
            'last_run':     last_run,
            'dot':          dot,
            'versions':     versions,
        })

    return rows


# ── Main tool page ─────────────────────────────────────────────────────────────

@login_required
def tool(request):
    """Verify Ads — summary dashboard (FIX 24/25/26)."""
    rows = _build_campaign_rows(request.user)
    return render(request, 'verification/tool.html', {'campaign_rows': rows})


# ── AJAX: run verification for a single campaign row ────────────────────────────

@login_required
def verify_row(request):
    """AJAX POST — run verification for one (account, channel, month) and return updated row data."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'})

    account_id = request.POST.get('account_id')
    channel    = request.POST.get('channel')
    month      = request.POST.get('month')
    mode       = request.POST.get('mode', 'smart')

    if not all([account_id, channel, month]):
        return JsonResponse({'ok': False, 'error': 'Missing parameters.'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied.'})

    try:
        run_scope(account_id, channel, month, mode=mode)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})

    # Return updated counts for this row
    mr_qs     = MatchResult.objects.filter(account_id=account_id, channel=channel, month=month)
    n_matched = mr_qs.filter(status='matched').count()
    n_missed  = mr_qs.filter(status='not_aired').count()
    active_ids = active_schedule_ids(account_id, channel, month)
    planned    = ScheduleRow.objects.filter(schedule_id__in=active_ids, ad_type='COMMERCIAL BENEFITS').count()
    dot       = _status_dot(account_id, channel, month)
    last_run  = mr_qs.order_by('-run_at').values_list('run_at', flat=True).first()

    return JsonResponse({
        'ok':       True,
        'planned':  planned,
        'aired':    n_matched,
        'missed':   n_missed,
        'dot':      dot,
        'last_run': last_run.strftime('%d %b %Y %H:%M') if last_run else None,
    })


# ── AJAX: load existing MatchResult rows for display ────────────────────────────

@login_required
def load_results(request):
    """GET — return persisted MatchResult rows for a scope, grouped by status."""
    account_id = request.GET.get('account_id')
    channel    = request.GET.get('channel')
    month      = request.GET.get('month')
    if not all([account_id, channel, month]):
        return JsonResponse({'ok': False, 'error': 'Missing params'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied'})

    STATUS_LABELS = dict(MatchResult.STATUS_CHOICES)
    qs = MatchResult.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).order_by('brand', 'scheduled_date', 'planned_start')

    def _row(mr):
        return {
            'brand':    mr.brand,
            'theme':    mr.theme,
            'duration': mr.duration,
            'programme': mr.programme,
            'planned_date':  str(mr.scheduled_date) if mr.scheduled_date else '',
            'planned_start': mr.planned_start or '',
            'planned_end':   mr.planned_end   or '',
            'aired_date':    str(mr.aired_date) if mr.aired_date else '',
            'air_time':      mr.air_time       or '',
            'source':        mr.source         or '',
            'status':        STATUS_LABELS.get(mr.status, mr.status),
        }

    all_rows       = [_row(r) for r in qs]
    matched        = [r for r in all_rows if r['status'] == 'Matched']
    prog_mismatch  = [r for r in all_rows if r['status'] == 'Programme Mismatch']
    late_telecast  = [r for r in all_rows if r['status'] == 'Late Telecast']
    not_aired      = [r for r in all_rows if r['status'] == 'Not Aired']
    extra          = [r for r in all_rows if r['status'] == 'Extra Aired']
    no_mapping     = [r for r in all_rows if r['status'] == 'No Brand Mapping']
    last_run = qs.order_by('-run_at').values_list('run_at', flat=True).first()

    return JsonResponse({
        'ok':            True,
        'has_results':   bool(all_rows),
        'total':         len(all_rows),
        'matched':       matched,
        'prog_mismatch': prog_mismatch,
        'late_telecast': late_telecast,
        'not_aired':     not_aired,
        'extra':         extra,
        'no_mapping':    no_mapping,
        'summary': {
            'total':         len(all_rows),
            'matched':       len(matched),
            'prog_mismatch': len(prog_mismatch),
            'late_telecast': len(late_telecast),
            'not_aired':     len(not_aired),
            'extra':         len(extra),
            'no_mapping':    len(no_mapping),
        },
        'last_run': last_run.strftime('%d %b %Y %H:%M') if last_run else None,
    })


# ── AJAX helpers ───────────────────────────────────────────────────────────────

@login_required
def get_channels(request):
    """Return channels that have BOTH schedule AND monitoring data for an account."""
    account_id = request.GET.get('account_id')
    if not account_id:
        return JsonResponse({'ok': False, 'error': 'No account specified'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied'})

    sch_channels = set(
        Schedule.objects.filter(account_id=account_id).values_list('channel', flat=True)
    )
    mon_channels = set(
        MonitoringData.objects.filter(account_id=account_id).values_list('channel', flat=True)
    )
    channels = sorted(sch_channels & mon_channels)
    return JsonResponse({'ok': True, 'channels': channels})


@login_required
def get_months(request):
    """Return schedule months for a given account + channel."""
    account_id = request.GET.get('account_id')
    channel    = request.GET.get('channel')
    if not account_id or not channel:
        return JsonResponse({'ok': False, 'error': 'Missing params'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied'})

    months = list(
        Schedule.objects.filter(account_id=account_id, channel=channel)
        .values_list('month', flat=True)
        .distinct()
        .order_by('month')
    )
    return JsonResponse({'ok': True, 'months': months})


@login_required
def get_preview(request):
    """Return schedule + monitoring info and brand mapping status for a selection."""
    from core.models import BrandMapping
    account_id = request.GET.get('account_id')
    channel    = request.GET.get('channel')
    month      = request.GET.get('month')
    if not all([account_id, channel, month]):
        return JsonResponse({'ok': False, 'error': 'Missing params'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied'})

    schedules = Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
    mon_data  = MonitoringData.objects.filter(account_id=account_id, channel=channel)
    mappings  = BrandMapping.objects.filter(account_id=account_id)

    return JsonResponse({
        'ok': True,
        'schedules': [
            {'schedule_number': s.schedule_number,
             'row_count':       s.row_count,
             'filename':        s.original_filename,
             'version':         s.version,
             'start_date':      str(s.start_date) if s.start_date else '',
             'end_date':        str(s.end_date)   if s.end_date   else ''}
            for s in schedules
        ],
        'monitoring': [
            {'data_type':   m.get_data_type_display(),
             'start_date':  str(m.start_date) if m.start_date else '',
             'end_date':    str(m.end_date)   if m.end_date   else '',
             'row_count':   m.row_count}
            for m in mon_data
        ],
        'mapping_count': mappings.count(),
        'brands':        sorted(set(mappings.values_list('brand', flat=True))),
        'ready':         schedules.exists() and mon_data.exists(),
    })


@login_required
def get_previous_results(request):
    """Return summary of existing MatchResults for a scope (smart-run info panel)."""
    account_id = request.GET.get('account_id')
    channel    = request.GET.get('channel')
    month      = request.GET.get('month')
    if not all([account_id, channel, month]):
        return JsonResponse({'ok': False, 'error': 'Missing params'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied'})

    qs = MatchResult.objects.filter(account_id=account_id, channel=channel, month=month)
    if not qs.exists():
        return JsonResponse({'ok': True, 'has_results': False})

    last_run = qs.order_by('-run_at').first()
    counts   = {s: 0 for s, _ in MatchResult.STATUS_CHOICES}
    for r in qs.values('status'):
        counts[r['status']] = counts.get(r['status'], 0) + 1

    return JsonResponse({
        'ok':                 True,
        'has_results':        True,
        'last_run_at':        last_run.run_at.strftime('%d %b %Y %H:%M'),
        'total':              qs.count(),
        'matched':            counts.get('matched', 0),
        'programme_mismatch': counts.get('programme_mismatch', 0),
        'late_telecast':      counts.get('late_telecast', 0),
        'not_aired':          counts.get('not_aired', 0),
        'no_mapping':         counts.get('no_mapping', 0),
    })


# ── Run verification ───────────────────────────────────────────────────────────

@login_required
def run_verification(request):
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'})

    account_id = request.POST.get('account_id')
    channel    = request.POST.get('channel')
    month      = request.POST.get('month')
    mode       = request.POST.get('mode', 'smart')

    if not all([account_id, channel, month]):
        return JsonResponse({'ok': False, 'error': 'Missing parameters.'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied.'})

    try:
        (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), total_sch = \
            run_scope(account_id, channel, month, mode=mode)

        n_matched   = len(matched_df)   if matched_df is not None and not matched_df.empty   else 0
        n_prog_mis  = len(prog_mis_df)  if prog_mis_df is not None and not prog_mis_df.empty  else 0
        n_late      = len(late_df)      if late_df is not None and not late_df.empty      else 0
        n_not_aired = len(not_aired_df) if not_aired_df is not None and not not_aired_df.empty else 0
        n_extra     = len(extra_df)     if extra_df is not None and not extra_df.empty     else 0

        n_aired    = n_matched + n_prog_mis + n_late
        compliance = round(n_matched / total_sch * 100, 1) if total_sch else 0

        return JsonResponse({
            'ok':            True,
            'mode':          mode,
            'matched':       _df_to_list(matched_df),
            'prog_mismatch': _df_to_list(prog_mis_df),
            'late_telecast': _df_to_list(late_df),
            'not_aired':     _df_to_list(not_aired_df),
            'extra':         _df_to_list(extra_df),
            'summary': {
                'total_scheduled': total_sch,
                'matched':         n_matched,
                'prog_mismatch':   n_prog_mis,
                'late_telecast':   n_late,
                'not_aired':       n_not_aired,
                'extra':           n_extra,
                'total_aired':     n_aired,
                'compliance':      compliance,
            },
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


# ── Exports ────────────────────────────────────────────────────────────────────

@login_required
def export_excel(request):
    """
    DB-based export for a specific (account, channel, month) scope.
    Reads MatchResult records — no engine re-run required.
    """
    account_id = request.POST.get('account_id') or request.GET.get('account_id')
    channel    = request.POST.get('channel')    or request.GET.get('channel')
    month      = request.POST.get('month')      or request.GET.get('month')

    if not _account_access(request.user, account_id):
        return HttpResponse('Access denied', status=403)

    qs = MatchResult.objects.filter(account_id=account_id, channel=channel, month=month)
    if not qs.exists():
        return HttpResponse(
            'No results found for this scope. Run verification first.',
            status=404,
        )

    filename = f'verification_{channel}_{month}'
    return _build_excel_response(qs, filename)


@login_required
def export_all(request):
    """
    DB-based export of ALL MatchResults for an account (all channels & months).
    Generates a single workbook with Full Report, status sheets, and summaries.
    """
    account_id = request.GET.get('account_id')
    if not account_id:
        return HttpResponse('Missing account_id', status=400)
    if not _account_access(request.user, account_id):
        return HttpResponse('Access denied', status=403)

    try:
        account = Account.objects.get(pk=account_id)
    except Account.DoesNotExist:
        return HttpResponse('Account not found', status=404)

    qs = MatchResult.objects.filter(account_id=account_id)
    if not qs.exists():
        return HttpResponse(
            'No results found for this account. Run verification first.',
            status=404,
        )

    filename = f'verification_all_{account.name}'
    return _build_excel_response(qs, filename)


# ── Report page ────────────────────────────────────────────────────────────────

@login_required
def report(request):
    """
    Global report page — per-scope (channel × month) summary from MatchResult.

    Query param: ?account_id=<id>
    """
    user = request.user
    if _is_admin(user):
        accounts = Account.objects.all().order_by('name')
    else:
        accounts = user.accounts.all().order_by('name')

    account_id       = request.GET.get('account_id')
    selected_account = None
    scopes           = []
    totals           = {}

    if account_id and _account_access(user, account_id):
        try:
            selected_account = Account.objects.get(pk=account_id)
        except Account.DoesNotExist:
            pass

    if selected_account:
        scope_combos = (
            MatchResult.objects.filter(account_id=account_id)
            .values('channel', 'month')
            .distinct()
            .order_by('channel', 'month')
        )
        for s in scope_combos:
            ch, mo = s['channel'], s['month']
            qs     = MatchResult.objects.filter(account_id=account_id, channel=ch, month=mo)
            total  = qs.count()
            counts = {k: 0 for k, _ in MatchResult.STATUS_CHOICES}
            for r in qs.values('status'):
                counts[r['status']] = counts.get(r['status'], 0) + 1
            last_run   = qs.order_by('-run_at').values_list('run_at', flat=True).first()
            n_matched  = counts.get('matched', 0)
            compliance = round(n_matched / total * 100, 1) if total else 0
            scopes.append({
                'channel':    ch,
                'month':      mo,
                'total':      total,
                'matched':    n_matched,
                'prog_mis':   counts.get('programme_mismatch', 0),
                'late':       counts.get('late_telecast', 0),
                'not_aired':  counts.get('not_aired', 0),
                'no_map':     counts.get('no_mapping', 0),
                'compliance': compliance,
                'last_run':   last_run,
            })

        # Account-level totals
        if scopes:
            totals = {
                'scopes':     len(scopes),
                'total':      sum(s['total']     for s in scopes),
                'matched':    sum(s['matched']   for s in scopes),
                'prog_mis':   sum(s['prog_mis']  for s in scopes),
                'late':       sum(s['late']      for s in scopes),
                'not_aired':  sum(s['not_aired'] for s in scopes),
                'no_map':     sum(s['no_map']    for s in scopes),
            }
            t = totals['total']
            totals['compliance'] = round(totals['matched'] / t * 100, 1) if t else 0

    return render(request, 'verification/report.html', {
        'accounts':         accounts,
        'selected_account': selected_account,
        'account_id':       account_id,
        'scopes':           scopes,
        'totals':           totals,
    })
