import io
import json
import pandas as pd
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from core.models import MonitoringData, Schedule
from .processing import match_ads, normalize, prepare_dataframes


@login_required
def tool(request):
    user = request.user
    # Build schedule and monitoring lists based on role
    sch_qs = Schedule.objects.select_related('account')
    mon_qs = MonitoringData.objects.all()
    if user.role == 'planner':
        sch_qs = sch_qs.filter(uploaded_by=user)
    elif user.role == 'team_head':
        sch_qs = sch_qs.filter(account__in=user.accounts.all())

    return render(request, 'verification/tool.html', {
        'schedules':       sch_qs,
        'monitoring_data': mon_qs,
    })


@login_required
@require_POST
def get_columns(request):
    """Return column names for a given uploaded file (called via AJAX)."""
    file_type = request.POST.get('file_type')   # 'schedule' | 'monitoring'
    obj_id    = request.POST.get('id')

    try:
        if file_type == 'schedule':
            obj  = Schedule.objects.get(pk=obj_id)
            file = obj.file
        else:
            obj  = MonitoringData.objects.get(pk=obj_id)
            file = obj.file

        df   = pd.read_excel(file.path)
        df.columns = df.columns.str.strip()
        themes = df.iloc[:, 0].unique().tolist()  # placeholder
        return JsonResponse({'columns': df.columns.tolist(), 'ok': True})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
@require_POST
def run_verification(request):
    """Run the matching engine. Returns JSON results."""
    try:
        sch_id         = request.POST.get('schedule_id')
        mon_id         = request.POST.get('monitoring_id')
        theme_mapping  = json.loads(request.POST.get('theme_mapping', '[]'))
        date_tolerance = int(request.POST.get('date_tolerance', 0))
        data_source    = request.POST.get('data_source', 'maponline')

        # Column selections from form
        mon_date_col  = request.POST.get('mon_date_col', 'Date')
        sch_date_col  = request.POST.get('sch_date_col', 'Date')
        mon_theme_col = request.POST.get('mon_theme_col', 'Advt_Theme')
        sch_theme_col = request.POST.get('sch_theme_col', 'Brand')
        mon_prog_col  = request.POST.get('mon_prog_col', 'Program')
        sch_prog_col  = request.POST.get('sch_prog_col', 'Programme')
        mon_dur_col   = request.POST.get('mon_dur_col', 'Dur')
        sch_dur_col   = request.POST.get('sch_dur_col', 'Duration')
        channel       = request.POST.get('channel', '')
        start_date    = request.POST.get('start_date', '')
        end_date      = request.POST.get('end_date', '')

        sch_obj = Schedule.objects.get(pk=sch_id)
        mon_obj = MonitoringData.objects.get(pk=mon_id)

        sch_df = pd.read_excel(sch_obj.file.path)
        mon_df = pd.read_excel(mon_obj.file.path)
        sch_df.columns = sch_df.columns.str.strip()
        mon_df.columns = mon_df.columns.str.strip()

        mon_df, sch_df = prepare_dataframes(
            mon_df, sch_df, data_source,
            mon_date_col, sch_date_col,
            mon_theme_col, sch_theme_col,
            mon_prog_col,  sch_prog_col,
            mon_dur_col,   sch_dur_col,
        )

        # Filter by channel and date range
        if channel and 'Channel' in mon_df.columns:
            mon_df = mon_df[mon_df['Channel'] == channel]
        if start_date:
            mon_df = mon_df[mon_df['Date'] >= pd.Timestamp(start_date)]
            sch_df = sch_df[sch_df['Date'] >= pd.Timestamp(start_date)]
        if end_date:
            mon_df = mon_df[mon_df['Date'] <= pd.Timestamp(end_date)]
            sch_df = sch_df[sch_df['Date'] <= pd.Timestamp(end_date)]

        matched, shifted, unmatched, extra = match_ads(sch_df, mon_df, theme_mapping, date_tolerance)

        def _to_list(df):
            if df.empty:
                return []
            # Convert Timestamps to strings
            for col in df.select_dtypes(include=['datetime64[ns]', 'datetime64[ns, UTC]']).columns:
                df[col] = df[col].dt.strftime('%Y-%m-%d')
            return df.fillna('').to_dict('records')

        total = len(matched) + len(shifted) + len(unmatched)
        return JsonResponse({
            'ok':         True,
            'matched':    _to_list(matched),
            'shifted':    _to_list(shifted),
            'unmatched':  _to_list(unmatched),
            'extra':      _to_list(extra),
            'summary': {
                'total':      total,
                'aired':      len(matched),
                'shifted':    len(shifted),
                'not_aired':  len(unmatched),
                'extra':      len(extra),
                'compliance': round(len(matched) / total * 100, 1) if total else 0,
            },
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
@require_POST
def get_themes(request):
    """Return unique themes for a schedule or monitoring dataset."""
    file_type = request.POST.get('file_type')
    obj_id    = request.POST.get('id')
    theme_col = request.POST.get('theme_col', 'Advt_Theme')
    try:
        if file_type == 'schedule':
            path = Schedule.objects.get(pk=obj_id).file.path
        else:
            path = MonitoringData.objects.get(pk=obj_id).file.path
        df     = pd.read_excel(path)
        df.columns = df.columns.str.strip()
        themes = sorted(df[theme_col].dropna().unique().tolist()) if theme_col in df.columns else []
        return JsonResponse({'ok': True, 'themes': [str(t) for t in themes]})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})
