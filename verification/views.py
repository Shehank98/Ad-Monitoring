"""
Verification engine views.

Supports two run modes:
  smart  (default) — check existing MatchResult records for this scope:
                      • skip schedule rows already marked 'matched'
                      • pre-lock LMRB rows consumed by any previous run
                      • process only new / previously unmatched rows
  reset             — delete all MatchResults for the scope first, then run fresh

After every run, results are persisted to MatchResult.
"""
import io
import pandas as pd
from datetime import date as date_cls

from django.contrib.auth.decorators import login_required
from django.db.models import Max, Min
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.models import Account, BrandMapping, MatchResult, MonitoringData, Schedule
from .processing import (
    lmrb_fingerprint, match_ads, normalize,
    prepare_monitoring_pool, prepare_schedule,
)


def _is_admin(user):
    return user.role in ('super_admin', 'admin')


def _account_access(user, account_id):
    if _is_admin(user):
        return True
    return user.accounts.filter(id=account_id).exists()


# ── Main tool page ─────────────────────────────────────────────────────────────

@login_required
def tool(request):
    user = request.user
    if _is_admin(user):
        accounts = Account.objects.all().order_by('name')
    else:
        accounts = user.accounts.all().order_by('name')
    return render(request, 'verification/tool.html', {'accounts': accounts})


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

    # Version info
    versions = list(
        schedules.values_list('version', 'uploaded_at', 'row_count', 'schedule_number')
                 .order_by('version')
    )

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
    """Return summary of existing MatchResults for a scope (for smart-run info panel)."""
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
        'ok':              True,
        'has_results':     True,
        'last_run_at':     last_run.run_at.strftime('%d %b %Y %H:%M'),
        'total':           qs.count(),
        'matched':         counts.get('matched', 0),
        'programme_mismatch': counts.get('programme_mismatch', 0),
        'late_telecast':   counts.get('late_telecast', 0),
        'not_aired':       counts.get('not_aired', 0),
        'no_mapping':      counts.get('no_mapping', 0),
    })


# ── Verification engine ────────────────────────────────────────────────────────

def _build_brand_theme_map(account_id):
    """Build the brand→[(theme, duration)] map from DB."""
    brand_theme_map = {}
    for bm in BrandMapping.objects.filter(account_id=account_id):
        norm_brand = normalize(bm.brand)
        norm_theme = normalize(bm.theme)
        mapping_dur = int(bm.duration) if bm.duration is not None else None
        brand_theme_map.setdefault(norm_brand, []).append((norm_theme, mapping_dur))
    return brand_theme_map


def _run_engine(account_id, channel, month, mode='smart'):
    """
    Load files, run matching, persist results.

    mode='smart': existing Matched results are kept; only new/unmatched rows are processed.
    mode='reset': all prior MatchResults for this scope are deleted; full re-run.

    Returns (matched_df, prog_mismatch_df, late_telecast_df, not_aired_df, extra_df), total_sch.
    """
    schedules = Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
    mon_qs    = MonitoringData.objects.filter(account_id=account_id, channel=channel)

    if not schedules.exists():
        raise ValueError(f'No schedule found for "{channel}" / "{month}".')
    if not mon_qs.exists():
        raise ValueError(f'No monitoring data found for channel "{channel}".')

    # ── Load schedule ──────────────────────────────────────────────────────────
    sch_frames = []
    for s in schedules:
        df = pd.read_excel(s.file.path)
        df.columns = df.columns.str.strip()
        sch_frames.append(df)
    sch_df = pd.concat(sch_frames, ignore_index=True)
    sch_df = prepare_schedule(sch_df)

    # Schedule date range (for LMRB filtering)
    sch_dates = schedules.aggregate(min=Min('start_date'), max=Max('end_date'))
    date_start = sch_dates['min']
    date_end   = sch_dates['max']

    # ── Load monitoring ────────────────────────────────────────────────────────
    mon_files = []
    for m in mon_qs:
        df = pd.read_excel(m.file.path)
        df.columns = df.columns.str.strip()
        mon_files.append((m.data_type, df))

    mon_pool = prepare_monitoring_pool(
        mon_files,
        channel_filter=channel,
        date_start=date_start,
        date_end=date_end,
    )

    brand_theme_map = _build_brand_theme_map(account_id)

    # ── Smart re-run: load previous results ───────────────────────────────────
    pre_matched_fp  = set()
    skip_sch_keys   = set()

    scope_qs = MatchResult.objects.filter(account_id=account_id, channel=channel, month=month)

    if mode == 'reset':
        scope_qs.delete()
    else:  # smart
        for mr in scope_qs:
            if mr.lmrb_fingerprint:
                pre_matched_fp.add(mr.lmrb_fingerprint)
            if mr.status == 'matched':
                skip_sch_keys.add((
                    normalize(mr.brand),
                    str(mr.scheduled_date),
                    mr.planned_start,
                    mr.planned_end,
                    str(mr.duration),
                ))

    # ── Run matching ──────────────────────────────────────────────────────────
    matched_df, prog_mis_df, late_df, not_aired_df, extra_df = match_ads(
        sch_df, mon_pool, brand_theme_map,
        pre_matched_fp=pre_matched_fp,
        skip_sch_keys=skip_sch_keys,
    )

    # ── Persist new results ───────────────────────────────────────────────────
    def _to_date(val):
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        try:
            ts = pd.Timestamp(val)
            return ts.date() if not pd.isna(ts) else None
        except Exception:
            return None

    def _str(val):
        return '' if (val is None or (isinstance(val, float) and pd.isna(val))) else str(val)

    def _int(val):
        try:
            return int(val) if val is not None and not pd.isna(val) else None
        except Exception:
            return None

    to_save = []
    account_obj = Account.objects.get(pk=account_id)

    for df, status_field in [
        (matched_df,   'matched'),
        (prog_mis_df,  'programme_mismatch'),
        (late_df,      'late_telecast'),
        (not_aired_df, 'not_aired'),
    ]:
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            raw_status = _str(r.get('Status', status_field))
            # Normalise status string to DB choice key
            status_map = {
                'matched':            'matched',
                'programme mismatch': 'programme_mismatch',
                'late telecast':      'late_telecast',
                'not aired':          'not_aired',
                'no brand mapping':   'no_mapping',
            }
            db_status = status_map.get(raw_status.lower(), status_field)

            to_save.append(MatchResult(
                account         = account_obj,
                channel         = channel,
                month           = month,
                brand           = _str(r.get('Brand', '')),
                programme       = _str(r.get('Programme', '')),
                scheduled_date  = _to_date(r.get('Scheduled_Date')),
                planned_start   = _str(r.get('Planned_Start', r.get('Start_Time', ''))),
                planned_end     = _str(r.get('Planned_End', r.get('End_Time', ''))),
                duration        = _int(r.get('Duration')),
                theme           = _str(r.get('Theme', '')),
                aired_date      = _to_date(r.get('Aired_Date')),
                air_time        = _str(r.get('Air_Time', '')),
                source          = _str(r.get('Source', '')),
                status          = db_status,
                lmrb_fingerprint = _str(r.get('_lmrb_fp', '')),
            ))

    if to_save:
        MatchResult.objects.bulk_create(to_save, batch_size=500)

    return (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), len(sch_df)


def _df_to_list(df):
    if df is None or df.empty:
        return []
    df = df.copy()
    # Drop internal helper columns before sending to client
    for col in ['_lmrb_fp', '_sch_key']:
        if col in df.columns:
            df.drop(columns=[col], inplace=True)
    for col in df.select_dtypes(include=['datetime64[ns]', 'datetime64[ns, UTC]']).columns:
        df[col] = df[col].dt.strftime('%Y-%m-%d')
    return df.fillna('').to_dict('records')


@login_required
@require_POST
def run_verification(request):
    account_id = request.POST.get('account_id')
    channel    = request.POST.get('channel')
    month      = request.POST.get('month')
    mode       = request.POST.get('mode', 'smart')  # 'smart' or 'reset'

    if not all([account_id, channel, month]):
        return JsonResponse({'ok': False, 'error': 'Missing parameters.'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied.'})

    try:
        (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), total_sch = \
            _run_engine(account_id, channel, month, mode=mode)

        n_matched   = len(matched_df)   if not matched_df.empty   else 0
        n_prog_mis  = len(prog_mis_df)  if not prog_mis_df.empty  else 0
        n_late      = len(late_df)      if not late_df.empty      else 0
        n_not_aired = len(not_aired_df) if not not_aired_df.empty else 0
        n_extra     = len(extra_df)     if not extra_df.empty     else 0

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
                'total_scheduled':   total_sch,
                'matched':           n_matched,
                'prog_mismatch':     n_prog_mis,
                'late_telecast':     n_late,
                'not_aired':         n_not_aired,
                'extra':             n_extra,
                'total_aired':       n_aired,
                'compliance':        compliance,
            },
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
@require_POST
def export_excel(request):
    account_id = request.POST.get('account_id')
    channel    = request.POST.get('channel')
    month      = request.POST.get('month')
    mode       = request.POST.get('mode', 'smart')

    if not _account_access(request.user, account_id):
        return HttpResponse('Access denied', status=403)

    try:
        (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), _ = \
            _run_engine(account_id, channel, month, mode=mode)

        def _clean(df):
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.copy()
            for col in ['_lmrb_fp', '_sch_key']:
                if col in df.columns:
                    df.drop(columns=[col], inplace=True)
            for col in df.select_dtypes(include=['datetime64[ns]', 'datetime64[ns, UTC]']).columns:
                df[col] = df[col].dt.strftime('%Y-%m-%d')
            return df.fillna('')

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            _clean(matched_df).to_excel(writer,   sheet_name='Matched',            index=False)
            _clean(prog_mis_df).to_excel(writer,  sheet_name='Programme Mismatch', index=False)
            _clean(late_df).to_excel(writer,      sheet_name='Late Telecast',      index=False)
            _clean(not_aired_df).to_excel(writer, sheet_name='Not Aired',          index=False)
            _clean(extra_df).to_excel(writer,     sheet_name='Extra Aired',        index=False)

        output.seek(0)
        filename = f'verification_{channel}_{month}.xlsx'.replace(' ', '_')
        response = HttpResponse(
            output.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        return HttpResponse(f'Export failed: {e}', status=500)
