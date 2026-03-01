import io
import json
import pandas as pd

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.models import Account, BrandMapping, MonitoringData, Schedule
from .processing import match_ads, normalize, prepare_monitoring_pool, prepare_schedule


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

    return JsonResponse({
        'ok': True,
        'schedules': [
            {'schedule_number': s.schedule_number,
             'row_count':       s.row_count,
             'filename':        s.original_filename}
            for s in schedules
        ],
        'monitoring': [
            {'data_type':   m.get_data_type_display(),
             'start_date':  str(m.start_date),
             'end_date':    str(m.end_date),
             'row_count':   m.row_count}
            for m in mon_data
        ],
        'mapping_count': mappings.count(),
        'brands':        sorted(set(mappings.values_list('brand', flat=True))),
        'ready':         schedules.exists() and mon_data.exists(),
    })


# ── Verification engine ────────────────────────────────────────────────────────

def _build_brand_theme_map(account_id):
    """
    Build the brand→[(theme, duration)] map from DB.
    duration is None when the mapping applies to any duration.
    """
    brand_theme_map = {}
    for bm in BrandMapping.objects.filter(account_id=account_id):
        norm_brand = normalize(bm.brand)
        norm_theme = normalize(bm.theme)
        mapping_dur = int(bm.duration) if bm.duration is not None else None
        brand_theme_map.setdefault(norm_brand, []).append((norm_theme, mapping_dur))
    return brand_theme_map


def _run_engine(account_id, channel, month):
    """Load files, run matching, return (matched, prog_mismatch, late_telecast, not_aired, extra), total_sch."""
    schedules = Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
    mon_qs    = MonitoringData.objects.filter(account_id=account_id, channel=channel)

    if not schedules.exists():
        raise ValueError(f'No schedule found for "{channel}" / "{month}".')
    if not mon_qs.exists():
        raise ValueError(f'No monitoring data found for channel "{channel}".')

    # Load + combine schedule files
    sch_frames = []
    for s in schedules:
        df = pd.read_excel(s.file.path)
        df.columns = df.columns.str.strip()
        sch_frames.append(df)
    sch_df = pd.concat(sch_frames, ignore_index=True)

    # Load + combine monitoring files
    mon_files = []
    for m in mon_qs:
        df = pd.read_excel(m.file.path)
        df.columns = df.columns.str.strip()
        mon_files.append((m.data_type, df))

    sch_df   = prepare_schedule(sch_df)
    mon_pool = prepare_monitoring_pool(mon_files)

    brand_theme_map = _build_brand_theme_map(account_id)

    results = match_ads(sch_df, mon_pool, brand_theme_map)
    return results, len(sch_df)


def _df_to_list(df):
    if df is None or df.empty:
        return []
    df = df.copy()
    for col in df.select_dtypes(include=['datetime64[ns]', 'datetime64[ns, UTC]']).columns:
        df[col] = df[col].dt.strftime('%Y-%m-%d')
    return df.fillna('').to_dict('records')


@login_required
@require_POST
def run_verification(request):
    account_id = request.POST.get('account_id')
    channel    = request.POST.get('channel')
    month      = request.POST.get('month')

    if not all([account_id, channel, month]):
        return JsonResponse({'ok': False, 'error': 'Missing parameters.'})
    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied.'})

    try:
        (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), total_sch = \
            _run_engine(account_id, channel, month)

        n_matched   = len(matched_df)   if not matched_df.empty   else 0
        n_prog_mis  = len(prog_mis_df)  if not prog_mis_df.empty  else 0
        n_late      = len(late_df)      if not late_df.empty      else 0
        n_not_aired = len(not_aired_df) if not not_aired_df.empty else 0
        n_extra     = len(extra_df)     if not extra_df.empty     else 0

        # Ads that were aired in some form (matched + mismatch + late)
        n_aired     = n_matched + n_prog_mis + n_late
        compliance  = round(n_matched / total_sch * 100, 1) if total_sch else 0

        return JsonResponse({
            'ok':            True,
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

    if not _account_access(request.user, account_id):
        return HttpResponse('Access denied', status=403)

    try:
        (matched_df, prog_mis_df, late_df, not_aired_df, extra_df), _ = \
            _run_engine(account_id, channel, month)

        def _clean(df):
            if df is None or df.empty:
                return pd.DataFrame()
            df = df.copy()
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
