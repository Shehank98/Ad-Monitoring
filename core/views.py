import io
import json
import os
import uuid
import pandas as pd
from datetime import date as date_cls, datetime

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Min, Count
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from accounts.models import User
from accounts.views import create_user, edit_user, user_list

from .forms import AccountForm, ChannelForm, MonitoringUploadForm, ScheduleUploadForm
from .models import (
    Account, BrandMapping, Channel,
    LMRBRow, MatchResult, MonitoringData, Schedule, ScheduleRow,
    SponsorshipLmrbAssignment, SummaryReportMeta, SystemSetting,
    TCRow, TransmissionReport,
    _ensure_defaults, get_setting_list,
)


# ── JSON serialisation helper ─────────────────────────────────────────────────

class _JsonEnc(json.JSONEncoder):
    """Handles date, datetime, and Decimal so view data serialises cleanly."""
    def default(self, o):
        from decimal import Decimal
        if isinstance(o, (date_cls, datetime)):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _to_js(data):
    """Serialize Python data to a JS-safe JSON string (replaces </script> escapes)."""
    return json.dumps(data, cls=_JsonEnc).replace('</', '<\\/')


def _theme_adtype(theme: str, brand_map: dict, spon_kw: list) -> str:
    """Classify a theme as 'commercial', 'sponsorship', or 'other'.

    Priority: BrandMapping lookup first, then keyword substring match.
    spon_kw is a list of lowercase keyword strings from SystemSetting.
    """
    key = (theme or '').lower().strip()
    tp = brand_map.get(key)
    if tp:
        return tp
    for kw in spon_kw:
        if kw and kw.lower() in key:
            return 'sponsorship'
    return 'other'


# ── Branding helpers ──────────────────────────────────────────────────────────

def _branding_url(asset_type: str) -> str:
    """Return the media URL for a branding asset (logo/tartan), or empty string if not uploaded."""
    branding_dir = os.path.join(django_settings.MEDIA_ROOT, 'branding')
    for ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'):
        path = os.path.join(branding_dir, f'{asset_type}{ext}')
        if os.path.exists(path):
            return django_settings.MEDIA_URL + f'branding/{asset_type}{ext}'
    return ''


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_admin(user):
    return user.role in ('super_admin', 'admin')


def _account_access(user, account_id):
    if _is_admin(user):
        return True
    return user.accounts.filter(id=account_id).exists()


def _account_qs(user):
    if _is_admin(user):
        return Account.objects.all()
    return user.accounts.all()


def _detect_schedule_meta(df):
    """Auto-detect month, start_date, end_date from a schedule DataFrame."""
    df = df.copy()
    df.columns = df.columns.str.strip()
    month = ''
    start_date = end_date = None
    if 'Date' in df.columns:
        dates = pd.to_datetime(df['Date'], errors='coerce').dropna()
        if not dates.empty:
            start_date = dates.min().date()
            end_date   = dates.max().date()
            month      = dates.min().strftime('%B %Y')
    return month, start_date, end_date


def _detect_monitoring_meta(df, data_type):
    """
    Auto-detect channels and per-channel date ranges from a monitoring DataFrame.

    Returns list of dicts: [{channel, start_date, end_date, row_count}, ...]
    """
    df = df.copy()
    df.columns = df.columns.str.strip()

    if data_type == 'mediawatch':
        if {'Dd', 'Mn', 'Yr'}.issubset(df.columns):
            df['_date'] = pd.to_datetime(
                df['Yr'].astype(str) + '-' +
                df['Mn'].astype(str).str.zfill(2) + '-' +
                df['Dd'].astype(str).str.zfill(2),
                errors='coerce',
            )
        elif 'Date' in df.columns:
            df['_date'] = pd.to_datetime(df['Date'], errors='coerce')
        else:
            df['_date'] = pd.NaT
    else:  # maponline
        date_col = 'Prg Date' if 'Prg Date' in df.columns else 'Date'
        df['_date'] = pd.to_datetime(df.get(date_col, pd.Series(dtype='object')), errors='coerce')

    ch_col = 'Channel' if 'Channel' in df.columns else None
    channels = [str(c).strip() for c in df[ch_col].dropna().unique() if str(c).strip()] \
               if ch_col else ['Unknown']

    result = []
    for ch in channels:
        ch_df  = df[df[ch_col].astype(str).str.strip() == ch] if ch_col else df
        dates  = ch_df['_date'].dropna()
        result.append({
            'channel':    ch,
            'start_date': dates.min().date() if not dates.empty else None,
            'end_date':   dates.max().date() if not dates.empty else None,
            'row_count':  len(ch_df),
        })
    return result


def _safe_int(val):
    try:
        if val is not None and not (isinstance(val, float) and pd.isna(val)):
            return int(float(val))
    except (ValueError, TypeError):
        pass
    return None


def _safe_str(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ''
    return str(val).strip()


def _safe_date(val):
    try:
        ts = pd.to_datetime(val, errors='coerce')
        return ts.date() if not pd.isna(ts) else None
    except Exception:
        return None


def _safe_decimal(val):
    from decimal import Decimal, InvalidOperation
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ── Dashboard ─────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    user = request.user
    role = user.role
    ctx  = {'user': user}

    if role in ('super_admin', 'admin'):
        ctx['total_users']      = User.objects.count()
        ctx['active_users']     = User.objects.filter(is_active=True).count()
        ctx['total_schedules']  = Schedule.objects.count()
        ctx['total_mon']        = MonitoringData.objects.count()
        ctx['recent_schedules'] = Schedule.objects.select_related('account', 'uploaded_by')[:5]
        ctx['recent_mon']       = MonitoringData.objects.select_related('uploaded_by', 'account')[:5]

    elif role == 'team_head':
        my_accounts = user.accounts.all()
        sch = Schedule.objects.filter(account__in=my_accounts).select_related('account')
        ctx['my_accounts']    = my_accounts
        ctx['schedules']      = sch[:5]
        ctx['schedule_count'] = sch.count()
        ctx['mon_count']      = MonitoringData.objects.filter(account__in=my_accounts).count()

    elif role == 'planner':
        my_accounts = user.accounts.all()
        sch = Schedule.objects.filter(account__in=my_accounts).select_related('account')
        ctx['my_accounts']    = my_accounts
        ctx['my_schedules']   = sch[:5]
        ctx['schedule_count'] = sch.count()

    elif role in ('operations', 'team_head'):
        my_accounts = user.accounts.all()
        mon = MonitoringData.objects.filter(account__in=my_accounts)
        ctx['my_uploads']   = mon[:5]
        ctx['upload_count'] = mon.filter(uploaded_by=user).count()
        ctx['total_mon']    = mon.count()

    return render(request, 'dashboard/home.html', ctx)


# ── Account management ────────────────────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin'])
def account_list(request):
    accounts = Account.objects.all()
    form     = AccountForm()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            form = AccountForm(request.POST)
            if form.is_valid():
                form.save()
                messages.success(request, f'Account "{form.cleaned_data["name"]}" added.')
                return redirect('/dashboard/accounts/')
        elif action == 'delete':
            acc_id = request.POST.get('account_id')
            acc    = get_object_or_404(Account, id=acc_id)
            acc.delete()
            messages.success(request, f'Account "{acc.name}" deleted.')
            return redirect('/dashboard/accounts/')

    return render(request, 'admin_panel/accounts.html', {'accounts': accounts, 'form': form})


# ── Channel management ────────────────────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin'])
def channel_list(request):
    channels = Channel.objects.all()
    form     = ChannelForm()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            form = ChannelForm(request.POST)
            if form.is_valid():
                form.save()
                messages.success(request, f'Channel "{form.cleaned_data["name"]}" added.')
                return redirect('/dashboard/channels/')
        elif action == 'delete':
            ch_id = request.POST.get('channel_id')
            ch    = get_object_or_404(Channel, id=ch_id)
            ch.delete()
            messages.success(request, f'Channel "{ch.name}" deleted.')
            return redirect('/dashboard/channels/')

    return render(request, 'admin_panel/channels.html', {'channels': channels, 'form': form})


# ── Schedules ─────────────────────────────────────────────────────────────────

@login_required
def schedule_list(request):
    user = request.user
    qs   = Schedule.objects.select_related('account', 'uploaded_by')
    if not _is_admin(user):
        qs = qs.filter(account__in=user.accounts.all())

    account_id = request.GET.get('account')
    channel    = request.GET.get('channel', '').strip()
    month      = request.GET.get('month', '').strip()
    if account_id:
        qs = qs.filter(account_id=account_id)
    if channel:
        qs = qs.filter(channel__icontains=channel)
    if month:
        qs = qs.filter(month__icontains=month)

    today    = date_cls.today()
    accounts = _account_qs(user)
    channels = Channel.objects.all()
    return render(request, 'schedules/list.html', {
        'schedules': qs,
        'accounts':  accounts,
        'channels':  channels,
        'filters':   {'account': account_id, 'channel': channel, 'month': month},
        'today':     today,
    })


@login_required
@role_required(['planner', 'super_admin', 'admin', 'team_head', 'operations'])
def schedule_upload(request):
    user = request.user
    form = ScheduleUploadForm()
    if not _is_admin(user):
        form.fields['account'].queryset = user.accounts.all()

    if request.method == 'POST':
        form = ScheduleUploadForm(request.POST, request.FILES)
        if not _is_admin(user):
            form.fields['account'].queryset = user.accounts.all()

        if form.is_valid():
            excel_file = request.FILES['file']
            try:
                from verification.schedule_converter import convert_schedule_excel
                df = convert_schedule_excel(excel_file)
                if df is None or df.empty:
                    # Not pivot format — fall back to plain flat read
                    excel_file.seek(0)
                    df = pd.read_excel(excel_file)
                    df.columns = df.columns.str.strip()
            except Exception as e:
                messages.error(request, f'Cannot read Excel file: {e}')
                return render(request, 'schedules/upload.html', {'form': form})

            row_count   = len(df)
            account     = form.cleaned_data['account']
            channel_obj = form.cleaned_data['channel']

            month, start_date, end_date = _detect_schedule_meta(df)
            if not month:
                month = 'Unknown'

            sched_number   = form.cleaned_data['schedule_number'].strip()
            channel_name   = channel_obj.name
            dup_action     = request.POST.get('duplicate_action', '')

            # Check for duplicate ref number within same (account, channel, month)
            existing_same_ref = Schedule.objects.filter(
                account=account, channel=channel_name,
                month=month, schedule_number=sched_number,
                is_superseded=False,
            ).first()

            if existing_same_ref and dup_action not in ('replace', 'keep_both'):
                # Re-render the form with a confirmation prompt
                return render(request, 'schedules/upload.html', {
                    'form':          form,
                    'schedule_col_guide': [
                        ('PROGRAMME',   'Programme / show name',              True),
                        ('DAY',         'Day abbreviation (MON, TUE…)',       True),
                        ('TIME',        'Ad start time (HH:MM:SS)',           True),
                        ('End Time',    'Ad end time — column after TIME',    False),
                        ('DUR',         'Duration in seconds',                True),
                        ('BRAND',       'Brand name (optional inline col)',   False),
                        ('[date cols]', 'Date values → spot count per day',   True),
                    ],
                    'dup_ref_existing': existing_same_ref,
                    'dup_ref_form_data': request.POST,
                    'dup_ref_file_key':  excel_file.name,
                })

            # Handle replacement: mark existing as superseded + release matched LMRB
            if existing_same_ref and dup_action == 'replace':
                from core.models import LMRBRow as _LR, ScheduleRow as _SR
                # Release matched LMRB rows linked to superseded schedule rows
                sr_ids = list(existing_same_ref.rows.values_list('id', flat=True))
                _LR.objects.filter(schedule_matches__in=sr_ids).update(
                    is_matched=False, matched_at=None
                )
                existing_same_ref.rows.update(
                    is_matched=False, matched_lmrb=None, matched_at=None,
                    is_manual_matched=False,
                )
                existing_same_ref.is_superseded = True
                existing_same_ref.save(update_fields=['is_superseded'])
                messages.warning(request,
                    f'Schedule #{sched_number} (v{existing_same_ref.version}) '
                    f'was superseded. Previously matched LMRB rows have been released for re-matching.')

            # Version = next version per (account, channel, month)
            last_ver = (
                Schedule.objects
                .filter(account=account, channel=channel_name, month=month)
                .aggregate(Max('version'))['version__max'] or 0
            )
            version = last_ver + 1

            excel_file.seek(0)
            schedule = Schedule(
                account           = account,
                channel           = channel_name,
                month             = month,
                schedule_number   = sched_number,
                original_filename = excel_file.name,
                row_count         = row_count,
                start_date        = start_date,
                end_date          = end_date,
                version           = version,
                uploaded_by       = user,
            )
            schedule.file.save(excel_file.name, excel_file)
            schedule.save()

            # ── Parse Schedule rows into DB ────────────────────────────────────
            _parse_schedule_rows(df, schedule, account, channel_name, month)

            messages.success(request,
                f'Schedule #{schedule.schedule_number} v{version} for {account} '
                f'({month}, {start_date} → {end_date}) uploaded — {row_count:,} rows.')

            # Auto-run verification for all available scopes
            try:
                from verification.engine import auto_run_all_for_account
                auto_run_all_for_account(account.id)
            except Exception:
                pass

            return redirect('/dashboard/schedules/')

    col_guide = [
        ('PROGRAMME',   'Programme / show name',              True),
        ('DAY',         'Day abbreviation (MON, TUE…)',       True),
        ('TIME',        'Ad start time (HH:MM:SS)',           True),
        ('End Time',    'Ad end time — column after TIME',    False),
        ('DUR',         'Duration in seconds',                True),
        ('BRAND',       'Brand name (optional inline col)',   False),
        ('[date cols]', 'Date values → spot count per day',   True),
    ]
    return render(request, 'schedules/upload.html', {'form': form, 'schedule_col_guide': col_guide})


def _parse_schedule_rows(df, schedule, account, channel, month):
    """Parse a schedule DataFrame and bulk-create ScheduleRow records."""
    rows = []
    for _, r in df.iterrows():
        ad_type = _safe_str(r.get('Advertisement_Type', '')).upper().strip()
        # Normalise: 'SPONSORSHIP BENEFITS' → 'SPONSORSHIP'
        if ad_type == 'SPONSORSHIP BENEFITS':
            ad_type = 'SPONSORSHIP'
        if ad_type not in ('COMMERCIAL BENEFITS', 'SPONSORSHIP'):
            continue
        rows.append(ScheduleRow(
            schedule   = schedule,
            account    = account,
            channel    = channel,
            month      = month,
            brand      = _safe_str(r.get('Brand', '')),
            programme  = _safe_str(r.get('Programme', '')),
            date       = _safe_date(r.get('Date')),
            start_time = _safe_str(r.get('Start_Time', '')),
            end_time   = _safe_str(r.get('End_Time', '')),
            duration   = _safe_int(r.get('Duration')),
            ad_type    = ad_type,
        ))
    if rows:
        ScheduleRow.objects.bulk_create(rows, batch_size=500)


@login_required
@require_POST
def schedule_detect(request):
    """AJAX: parse an uploaded schedule file and return detected metadata."""
    excel_file = request.FILES.get('file')
    if not excel_file:
        return JsonResponse({'ok': False, 'error': 'No file provided'})
    try:
        from verification.schedule_converter import convert_schedule_excel
        df = convert_schedule_excel(excel_file)
        pivot = df is not None and not df.empty
        if not pivot:
            excel_file.seek(0)
            df = pd.read_excel(excel_file)
        month, start_date, end_date = _detect_schedule_meta(df)
        return JsonResponse({
            'ok':         True,
            'month':      month,
            'start_date': str(start_date) if start_date else '',
            'end_date':   str(end_date)   if end_date   else '',
            'row_count':  len(df),
            'is_pivot':   pivot,
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
def schedule_download(request, pk):
    """Serve the original schedule Excel file as a download."""
    schedule = get_object_or_404(Schedule, pk=pk)
    if not _is_admin(request.user) and schedule.account not in _account_qs(request.user):
        return HttpResponse('Access denied', status=403)
    try:
        file_path = schedule.file.path
        if not os.path.exists(file_path):
            return HttpResponse('File not found on server.', status=404)
        return FileResponse(
            open(file_path, 'rb'),
            as_attachment=True,
            filename=schedule.original_filename or os.path.basename(file_path),
        )
    except Exception as e:
        return HttpResponse(f'Download failed: {e}', status=500)


@login_required
def schedule_delete(request, pk):
    schedule = get_object_or_404(Schedule, pk=pk)
    user     = request.user
    today    = date_cls.today()

    if _is_admin(user) or (schedule.uploaded_by == user and schedule.uploaded_at.date() == today):
        # Before deleting: clean up all associated data to maintain integrity
        sch_row_ids = list(schedule.rows.values_list('id', flat=True))

        if sch_row_ids:
            # Unlock any LMRBRows matched to schedule rows in this schedule
            LMRBRow.objects.filter(matched_schedule_id__in=sch_row_ids).update(
                is_matched=False, matched_schedule=None, matched_at=None,
            )
            # Reset TCRow schedule-match state for these schedule rows
            TCRow.objects.filter(matched_schedule_id__in=sch_row_ids).update(
                is_schedule_matched=False, matched_schedule=None,
            )
            # Delete MatchResult records for this schedule's rows
            MatchResult.objects.filter(schedule_row_id__in=sch_row_ids).delete()

        # Also delete MatchResult records scoped to this channel/month (engine-level)
        MatchResult.objects.filter(
            account=schedule.account, channel=schedule.channel, month=schedule.month,
        ).exclude(status='manual_match').delete()

        schedule.file.delete(save=False)
        schedule.delete()
        messages.success(request, 'Schedule deleted.')
    else:
        messages.error(request, 'You can only delete schedules you uploaded today.')
    return redirect('/dashboard/schedules/')


# ── Monitoring data ───────────────────────────────────────────────────────────

@login_required
def monitoring_list(request):
    user = request.user
    qs   = MonitoringData.objects.select_related('account', 'uploaded_by')
    if not _is_admin(user):
        qs = qs.filter(account__in=user.accounts.all())

    dtype      = request.GET.get('type', '')
    channel    = request.GET.get('channel', '').strip()
    account_id = request.GET.get('account', '')
    if dtype:
        qs = qs.filter(data_type=dtype)
    if channel:
        qs = qs.filter(channel__icontains=channel)
    if account_id:
        qs = qs.filter(account_id=account_id)

    # Group records by file_group_id (newest first) so multi-channel uploads
    # appear together and can be deleted as one unit.
    sorted_data = list(qs.order_by('-uploaded_at'))
    groups_dict  = {}
    groups_order = []
    for d in sorted_data:
        fgid = d.file_group_id
        if fgid not in groups_dict:
            groups_dict[fgid]  = []
            groups_order.append(fgid)
        groups_dict[fgid].append(d)
    data_groups = [groups_dict[fgid] for fgid in groups_order]

    print(f"[monitoring_list] total_records={len(sorted_data)}  groups={len(data_groups)}")
    for i, grp in enumerate(data_groups):
        chs = [d.channel for d in grp]
        print(f"  group {i+1}: file_group_id={grp[0].file_group_id}  channels={chs}  uploaded_at={grp[0].uploaded_at}")

    cov_base = MonitoringData.objects.select_related('account')
    if not _is_admin(user):
        cov_base = cov_base.filter(account__in=user.accounts.all())
    coverage = (
        cov_base
        .values('account__name', 'channel', 'data_type')
        .annotate(from_date=Min('start_date'), to_date=Max('end_date'), uploads=Count('id'))
        .order_by('data_type', 'account__name', 'channel')
    )

    # ── Build batch summaries for display (one row per file_group_id) ─────────
    batch_summaries = []
    for fgid in groups_order:
        grp = groups_dict[fgid]
        first = grp[0]
        total_rows = sum(d.row_count or 0 for d in grp)
        all_dates = [(d.start_date, d.end_date) for d in grp if d.start_date and d.end_date]
        date_from = min(s for s, e in all_dates) if all_dates else None
        date_to   = max(e for s, e in all_dates) if all_dates else None
        ch_list   = sorted(set(d.channel for d in grp if d.channel))
        batch_summaries.append({
            'file_group_id': fgid,
            'first':         first,
            'data_type':     first.data_type,
            'account':       first.account,
            'channels':      ch_list,
            'date_from':     date_from,
            'date_to':       date_to,
            'total_rows':    total_rows,
            'uploaded_by':   first.uploaded_by,
            'uploaded_at':   first.uploaded_at,
            'group':         grp,         # still available for per-channel download
        })

    today    = date_cls.today()
    accounts = _account_qs(user)
    channels = Channel.objects.all()
    return render(request, 'monitoring/list.html', {
        'data_groups':     data_groups,
        'batch_summaries': batch_summaries,
        'coverage':        coverage,
        'filters':         {'type': dtype, 'channel': channel, 'account': account_id},
        'accounts':        accounts,
        'channels':        channels,
        'today':           today,
    })


@login_required
@role_required(['operations', 'super_admin', 'admin', 'team_head', 'planner'])
def monitoring_upload(request):
    """
    Upload a MapOnline or LMRB file.

    Channels and date ranges are AUTO-DETECTED from the file.
    One MonitoringData record is created per detected channel.
    Individual rows are parsed into LMRBRow (master table) with deduplication.
    """
    user       = request.user
    account_qs = _account_qs(user)
    form       = MonitoringUploadForm(account_queryset=account_qs)

    if request.method == 'POST':
        form = MonitoringUploadForm(request.POST, request.FILES, account_queryset=account_qs)
        if form.is_valid():
            excel_file = request.FILES['file']
            data_type  = form.cleaned_data['data_type']
            account    = form.cleaned_data['account']
            print(f"[monitoring_upload] file={excel_file.name}  data_type={data_type}  account={account}")
            try:
                df = pd.read_excel(excel_file)
                df.columns = df.columns.str.strip()
                print(f"[monitoring_upload] Excel loaded: {len(df)} rows  columns={list(df.columns)}")
            except Exception as e:
                print(f"[monitoring_upload] ERROR reading Excel: {e}")
                messages.error(request, f'Cannot read Excel file: {e}')
                return render(request, 'monitoring/upload.html', {'form': form})

            channel_metas = _detect_monitoring_meta(df, data_type)
            print(f"[monitoring_upload] detected {len(channel_metas)} channel(s): {channel_metas}")
            if not channel_metas:
                print("[monitoring_upload] ERROR: no channels detected")
                messages.error(request, 'No channels detected in the file.')
                return render(request, 'monitoring/upload.html', {'form': form})

            for meta in channel_metas:
                ch = meta['channel']
                if ch and ch != 'Unknown':
                    # Case-insensitive lookup first to avoid creating duplicates
                    # when LMRB file uses "SIRASA TV" but Channel model has "Sirasa TV".
                    if not Channel.objects.filter(name__iexact=ch).exists():
                        Channel.objects.create(name=ch)

            group_id   = str(uuid.uuid4())
            saved_path = None
            excel_file.seek(0)
            print(f"[monitoring_upload] file_group_id={group_id}")

            for i, meta in enumerate(channel_metas):
                mon = MonitoringData(
                    account           = account,
                    data_type         = data_type,
                    channel           = meta['channel'],
                    start_date        = meta['start_date'],
                    end_date          = meta['end_date'],
                    original_filename = excel_file.name,
                    row_count         = meta['row_count'],
                    file_group_id     = group_id,
                    uploaded_by       = user,
                )
                if saved_path is None:
                    excel_file.seek(0)
                    mon.file.save(excel_file.name, excel_file, save=False)
                    saved_path = mon.file.name
                    print(f"[monitoring_upload] file saved → {saved_path}")
                else:
                    mon.file = saved_path
                mon.save()
                print(f"[monitoring_upload]   saved MonitoringData pk={mon.pk}  channel={meta['channel']}  rows={meta['row_count']}")

            # ── Parse LMRB rows into master DB table (append mode) ────────────
            print(f"[monitoring_upload] parsing LMRB rows (append mode) …")
            _parse_lmrb_rows(df, data_type, account, batch_id=uuid.UUID(group_id))

            ch_names = ', '.join(m['channel'] for m in channel_metas)
            print(f"[monitoring_upload] DONE — {len(channel_metas)} channel(s): {ch_names}")
            messages.success(request,
                f'{"MapOnline" if data_type == "maponline" else "MediaWatch (LMRB)"} — '
                f'{account} — {len(channel_metas)} channel(s): {ch_names}. Uploaded successfully.')

            # Auto-run verification
            try:
                from verification.engine import auto_run_all_for_account
                print(f"[monitoring_upload] auto-running verification for account {account.id}")
                auto_run_all_for_account(account.id)
            except Exception as e:
                print(f"[monitoring_upload] auto-verification error (non-fatal): {e}")

            return redirect('/dashboard/monitoring/')

    return render(request, 'monitoring/upload.html', {'form': form})


def _parse_lmrb_rows(df, data_type, account, batch_id=None):
    """
    Parse monitoring DataFrame and append into the LMRBRow master table.

    APPEND MODE — rows are never deleted or replaced.
    Dedup key = SHA-256 of all meaningful columns.  If a row with the same key
    already exists it is silently skipped (bulk_create with ignore_conflicts=True).
    This means:
      • Uploading the same file twice → zero new rows (all skipped as duplicates)
      • Uploading a different date range → only truly new rows are inserted
      • Historical data is never erased by re-uploads

    batch_id: UUID (MonitoringData.file_group_id) so a batch can be bulk-deleted
    later via monitoring_delete_group.
    """
    df = df.copy()
    df.columns = df.columns.str.strip()  # ensure columns are stripped

    # ── Build a case-insensitive channel name lookup from the Channel model ─────
    _ch_canonical = {c.lower(): c for c in Channel.objects.values_list('name', flat=True)}

    def _canon_channel(raw: str) -> str:
        raw = str(raw).strip()
        canonical = _ch_canonical.get(raw.lower())
        if canonical:
            return canonical
        for prefix in ('tv - ', 'radio - '):
            if raw.lower().startswith(prefix):
                stripped = raw[len(prefix):]
                canonical = _ch_canonical.get(stripped.lower())
                if canonical:
                    return canonical
                return stripped
        return raw

    # ── Normalise to standard column names ─────────────────────────────────────
    if data_type == 'maponline':
        rename = {}
        _lmrb_ex_theme = get_setting_list('lmrb_extra_theme_aliases')
        _lmrb_ex_time  = get_setting_list('lmrb_extra_time_aliases')
        _lmrb_ex_dur   = get_setting_list('lmrb_extra_duration_aliases')
        _lmrb_ex_date  = get_setting_list('lmrb_extra_date_aliases')
        th_col = _find_col(df, 'Advt_Theme', 'Theme', *_lmrb_ex_theme)
        if th_col and th_col != 'Advt_Theme': rename[th_col] = 'Advt_Theme'
        dt_col = _find_col(df, 'Date', 'Prg Date', *_lmrb_ex_date)
        if dt_col and dt_col != 'Date': rename[dt_col] = 'Date'
        du_col = _find_col(df, 'Dur', 'Ad Dur', *_lmrb_ex_dur)
        if du_col and du_col != 'Dur': rename[du_col] = 'Dur'
        tm_col = _find_col(df, 'Advt_time', 'Ad Start', 'Advt_Time', *_lmrb_ex_time)
        if tm_col and tm_col != 'Advt_time': rename[tm_col] = 'Advt_time'
        pg_col = _find_col(df, 'Programme', 'Prg Name', 'Program')
        if pg_col and pg_col != 'Programme': rename[pg_col] = 'Programme'
        if rename:
            df.rename(columns=rename, inplace=True)
        if _find_col(df, 'Date') is not None:
            df['Date'] = pd.to_datetime(df[_find_col(df, 'Date')], errors='coerce')
    else:  # mediawatch
        if {'Dd', 'Mn', 'Yr'}.issubset(df.columns) and _find_col(df, 'Date') is None:
            df['Date'] = pd.to_datetime(
                df['Yr'].astype(str) + '-' +
                df['Mn'].astype(str).str.zfill(2) + '-' +
                df['Dd'].astype(str).str.zfill(2),
                errors='coerce',
            )
        elif _find_col(df, 'Date') is not None:
            df['Date'] = pd.to_datetime(df[_find_col(df, 'Date')], errors='coerce')

    dur_col = _find_col(df, 'Dur', 'Ad Dur', 'Duration')
    if dur_col is not None:
        df[dur_col] = pd.to_numeric(df[dur_col], errors='coerce')
        if dur_col != 'Dur':
            df.rename(columns={dur_col: 'Dur'}, inplace=True)

    ch_col = _find_col(df, 'Channel', 'Station')

    # ── Build rows dict keyed by dedup_key ────────────────────────────────────
    rows_by_key = {}

    for _, r in df.iterrows():
        channel   = _canon_channel(_safe_str(r.get(ch_col, 'Unknown')) if ch_col else 'Unknown')
        theme     = _safe_str(r.get('Advt_Theme', ''))
        advt_time = _safe_str(r.get('Advt_time', ''))
        dur       = _safe_int(r.get('Dur'))
        date_val  = _safe_date(r.get('Date'))
        brk_no    = _safe_int(r.get('BrkNo'))
        pos_in_brk = _safe_int(r.get('PosinBrk'))
        advertiser = _safe_str(r.get('Advertiser', ''))
        product    = _safe_str(r.get('Product', ''))

        if not (theme and advt_time and date_val and channel):
            continue  # skip rows with missing key fields

        dedup_key = LMRBRow.make_dedup_key(
            account.id, channel, date_val, advt_time, theme, dur,
            brk_no=brk_no, pos_in_brk=pos_in_brk,
            advertiser=advertiser, product=product,
        )

        program_val = _safe_str(r.get('Program', r.get('Programme', r.get('Prg Name', ''))))

        # If same key appears twice in this upload, last row wins
        rows_by_key[dedup_key] = LMRBRow(
            account       = account,
            channel       = channel,
            date          = date_val,
            advt_theme    = theme,
            advt_time     = advt_time,
            duration      = dur,
            source        = data_type,
            dedup_key     = dedup_key,
            batch_id      = batch_id,
            # Extended columns
            product_group = _safe_str(r.get('Product_Group', '')),
            advertiser    = advertiser,
            product       = product,
            ads           = _safe_str(r.get('Ads', '')),
            program       = program_val,
            prog_time     = _safe_str(r.get('Prog_time', '')),
            ad_pos        = _safe_int(r.get('AdPos')),
            tot_ads       = _safe_int(r.get('TotAds')),
            brk_no        = brk_no,
            pos_in_brk    = pos_in_brk,
            ads_in_brk    = _safe_int(r.get('AdsinBrk')),
            lng           = _safe_str(r.get('Lng', '')),
            cost          = _safe_decimal(r.get('Cost')),
            day           = _safe_str(r.get('Day', '')),
        )

    print(f"[_parse_lmrb_rows] df rows={len(df)}  unique dedup_keys={len(rows_by_key)}")
    if not rows_by_key:
        print("[_parse_lmrb_rows] WARNING: no valid rows to insert (check column names and key fields)")
        return 0

    # ── Append-only insert — skip any rows that already exist ─────────────────
    new_rows = list(rows_by_key.values())
    created = LMRBRow.objects.bulk_create(new_rows, batch_size=500, ignore_conflicts=True)
    inserted = len(created)
    skipped  = len(new_rows) - inserted
    print(f"[_parse_lmrb_rows] inserted={inserted}  skipped(duplicate)={skipped}")
    return inserted


@login_required
@require_POST
def monitoring_detect(request):
    """AJAX: parse an uploaded monitoring file and return detected channels/dates."""
    excel_file = request.FILES.get('file')
    data_type  = request.POST.get('data_type', 'mediawatch')
    if not excel_file:
        return JsonResponse({'ok': False, 'error': 'No file provided'})
    try:
        df = pd.read_excel(excel_file)
        df.columns = df.columns.str.strip()
        metas = _detect_monitoring_meta(df, data_type)
        return JsonResponse({'ok': True, 'channels': metas, 'total_rows': len(df)})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
def monitoring_download(request, pk):
    """Serve the original monitoring Excel file as a download."""
    mon  = get_object_or_404(MonitoringData, pk=pk)
    user = request.user
    if not _is_admin(user) and mon.account not in _account_qs(user):
        return HttpResponse('Access denied', status=403)
    try:
        file_path = mon.file.path
        if not os.path.exists(file_path):
            return HttpResponse('File not found on server.', status=404)
        return FileResponse(
            open(file_path, 'rb'),
            as_attachment=True,
            filename=mon.original_filename or os.path.basename(file_path),
        )
    except Exception as e:
        return HttpResponse(f'Download failed: {e}', status=500)


@login_required
def monitoring_delete(request, pk):
    mon   = get_object_or_404(MonitoringData, pk=pk)
    user  = request.user
    today = date_cls.today()
    print(f"[monitoring_delete] pk={pk}  channel={mon.channel}  file_group_id={mon.file_group_id}  user={user}")

    if not _is_admin(user) and mon.account and mon.account not in _account_qs(user):
        print(f"[monitoring_delete] ACCESS DENIED — user has no access to account {mon.account}")
        messages.error(request, 'You do not have access to this data.')
        return redirect('/dashboard/monitoring/')

    if _is_admin(user) or (mon.uploaded_by == user and mon.uploaded_at.date() == today):
        siblings = MonitoringData.objects.filter(file_group_id=mon.file_group_id).exclude(pk=mon.pk)
        sibling_count = siblings.count()
        print(f"[monitoring_delete] siblings in same group={sibling_count}")
        if not siblings.exists():
            print(f"[monitoring_delete] no siblings — deleting shared file {mon.file.name}")
            mon.file.delete(save=False)
        mon.delete()
        print(f"[monitoring_delete] deleted MonitoringData pk={pk}")
        messages.success(request, 'Dataset deleted.')
    else:
        print(f"[monitoring_delete] DENIED — not admin and not uploaded today by this user")
        messages.error(request, 'You can only delete datasets you uploaded today.')
    return redirect('/dashboard/monitoring/')


@login_required
def monitoring_delete_group(request, group_id):
    """
    Delete ALL MonitoringData records that share the same file_group_id.
    This lets users remove an entire multi-channel upload in one click.
    """
    user  = request.user
    today = date_cls.today()
    print(f"[monitoring_delete_group] group_id={group_id}  user={user}")

    group_qs = MonitoringData.objects.filter(file_group_id=group_id).select_related('account', 'uploaded_by')
    if not group_qs.exists():
        print(f"[monitoring_delete_group] ERROR: no records found for group_id={group_id}")
        messages.error(request, 'No records found for this upload group.')
        return redirect('/dashboard/monitoring/')

    first    = group_qs.first()
    channels = list(group_qs.values_list('channel', flat=True))
    count    = group_qs.count()
    print(f"[monitoring_delete_group] found {count} record(s): channels={channels}")
    print(f"[monitoring_delete_group] first.account={first.account}  first.uploaded_by={first.uploaded_by}  first.uploaded_at={first.uploaded_at}")

    if not _is_admin(user) and first.account and first.account not in _account_qs(user):
        print(f"[monitoring_delete_group] ACCESS DENIED — user has no access to account {first.account}")
        messages.error(request, 'You do not have access to this data.')
        return redirect('/dashboard/monitoring/')

    if not _is_admin(user) and not (first.uploaded_by == user and first.uploaded_at.date() == today):
        print(f"[monitoring_delete_group] DENIED — not admin and not uploaded today by this user")
        messages.error(request, 'You can only delete datasets you uploaded today.')
        return redirect('/dashboard/monitoring/')

    # Delete LMRBRows that belong to this upload batch
    import uuid as uuid_mod
    from core.models import ManualMatch, SponsorshipLmrbAssignment
    try:
        batch_uuid = uuid_mod.UUID(str(group_id))
        lmrb_qs = LMRBRow.objects.filter(batch_id=batch_uuid)
        lmrb_ids = list(lmrb_qs.values_list('id', flat=True))

        # 1. Unlock matched ScheduleRows before deleting
        matched_sch_ids = list(
            lmrb_qs.filter(is_matched=True).values_list('matched_schedule_id', flat=True)
        )
        if matched_sch_ids:
            ScheduleRow.objects.filter(id__in=matched_sch_ids).update(
                is_matched=False, matched_lmrb=None, matched_at=None,
            )

        # 2. Unlock TCRows that were LMRB-confirmed via these rows
        if lmrb_ids:
            from core.models import TCRow
            TCRow.objects.filter(matched_lmrb_id__in=lmrb_ids).update(
                is_lmrb_confirmed=False, matched_lmrb=None,
            )

        # 3. Remove ManualMatch records using these LMRB rows + unlock their ScheduleRows
        if lmrb_ids:
            manual_matches = ManualMatch.objects.filter(lmrb_row_id__in=lmrb_ids)
            mm_sch_ids = list(manual_matches.exclude(schedule_row=None).values_list('schedule_row_id', flat=True))
            if mm_sch_ids:
                ScheduleRow.objects.filter(id__in=mm_sch_ids).update(is_manual_matched=False)
            manual_matches.delete()

        # 4. Remove SponsorshipLmrbAssignment records + unlock their ScheduleRows
        if lmrb_ids:
            spon_assignments = SponsorshipLmrbAssignment.objects.filter(lmrb_row_id__in=lmrb_ids)
            spon_sch_ids = list(spon_assignments.values_list('schedule_row_id', flat=True))
            spon_assignments.delete()
            # Unlock sponsorship ScheduleRows (no is_matched for sponsorship, handled via assignment)

        # 5. Delete MatchResult records linked to these LMRB rows
        if lmrb_ids:
            from core.models import MatchResult
            MatchResult.objects.filter(lmrb_row_id__in=lmrb_ids).delete()
            # Also delete MatchResult records for ScheduleRows that were matched to these LMRBs
            if matched_sch_ids:
                MatchResult.objects.filter(schedule_row_id__in=matched_sch_ids,
                                           status='matched').delete()

        lmrb_count = lmrb_qs.count()
        lmrb_qs.delete()
        print(f"[monitoring_delete_group] deleted {lmrb_count} LMRBRow(s) for batch {group_id}")
    except Exception as e:
        print(f"[monitoring_delete_group] WARNING: could not delete LMRBRows: {e}")
        lmrb_count = 0

    # Delete the shared file once (all records in the group share the same file)
    if first.file:
        print(f"[monitoring_delete_group] deleting shared file: {first.file.name}")
        first.file.delete(save=False)

    group_qs.delete()
    print(f"[monitoring_delete_group] deleted {count} MonitoringData record(s) successfully")
    messages.success(request,
        f'Deleted upload batch ({", ".join(channels)}) — '
        f'{lmrb_count:,} LMRB row(s) removed.')
    return redirect('/dashboard/monitoring/')


# ── Brand mappings ────────────────────────────────────────────────────────────

@login_required
def brand_mapping_list(request):
    user       = request.user
    account_qs = _account_qs(user)
    account_id = request.GET.get('account', '')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            acc_id   = request.POST.get('account_id', '').strip()
            brand    = request.POST.get('brand', '').strip()
            theme    = request.POST.get('theme', '').strip()
            tc_theme = request.POST.get('tc_theme', '').strip()
            dur_raw  = request.POST.get('duration', '').strip()
            duration = int(dur_raw) if dur_raw.isdigit() else None

            if not (acc_id and brand and theme):
                messages.error(request, 'Account, Brand, and LMRB Theme are all required.')
            else:
                account = get_object_or_404(Account, id=acc_id)
                if not _is_admin(user) and account not in account_qs:
                    messages.error(request, 'No access to that account.')
                else:
                    exists = BrandMapping.objects.filter(
                        account=account, brand=brand, theme=theme, duration=duration
                    ).exists()
                    if exists:
                        messages.warning(request, 'That mapping already exists.')
                    else:
                        BrandMapping.objects.create(
                            account=account, brand=brand, theme=theme,
                            tc_theme=tc_theme, duration=duration)
                        dur_str = f' ({duration}s)' if duration else ''
                        messages.success(request, f'Mapping added: {brand} → {theme}{dur_str}')
            return redirect(f'/dashboard/brand-mappings/?account={acc_id}')

        elif action == 'edit_tc_theme':
            mapping_id = request.POST.get('mapping_id')
            tc_theme   = request.POST.get('tc_theme', '').strip()
            mapping    = get_object_or_404(BrandMapping, id=mapping_id)
            if not _is_admin(user) and mapping.account not in account_qs:
                messages.error(request, 'No access to that mapping.')
            else:
                mapping.tc_theme = tc_theme
                mapping.save(update_fields=['tc_theme'])
                messages.success(request, f'TC Theme updated for {mapping.brand}.')
            return redirect(f'/dashboard/brand-mappings/?account={account_id or mapping.account_id}')

        elif action == 'delete':
            mapping_id = request.POST.get('mapping_id')
            mapping    = get_object_or_404(BrandMapping, id=mapping_id)
            saved_acc  = mapping.account_id
            if not _is_admin(user) and mapping.account not in account_qs:
                messages.error(request, 'No access to that mapping.')
            else:
                mapping.delete()
                messages.success(request, 'Mapping deleted.')
            return redirect(f'/dashboard/brand-mappings/?account={account_id or saved_acc}')

    mappings = BrandMapping.objects.filter(account__in=account_qs).select_related('account')
    channel_filter = request.GET.get('channel', '')
    month_filter   = request.GET.get('month', '')
    if account_id:
        mappings = mappings.filter(account_id=account_id)
    if channel_filter:
        mappings = mappings.filter(
            brand__in=ScheduleRow.objects.filter(
                account_id=account_id, schedule__channel=channel_filter,
            ).values_list('brand', flat=True)
        ) if account_id else mappings
    if month_filter:
        mappings = mappings.filter(
            brand__in=ScheduleRow.objects.filter(
                account_id=account_id, schedule__month=month_filter,
            ).values_list('brand', flat=True)
        ) if account_id else mappings

    # Channels available for the selected account (for table filter)
    channels_for_account = sorted(set(
        Schedule.objects.filter(account_id=account_id).values_list('channel', flat=True)
    )) if account_id else []
    months_for_channel = sorted(set(
        Schedule.objects.filter(
            account_id=account_id, channel=channel_filter,
        ).values_list('month', flat=True)
    )) if account_id and channel_filter else []

    return render(request, 'admin_panel/brand_mappings.html', {
        'mappings':             mappings,
        'accounts':             account_qs,
        'filters':              {
            'account': account_id,
            'channel': channel_filter,
            'month':   month_filter,
        },
        'channels_for_account': channels_for_account,
        'months_for_channel':   months_for_channel,
    })


@login_required
def brand_mapping_options(request):
    """
    AJAX: Return unique brands, LMRB themes, and TC themes for an account.
    Narrows results when channel / month / schedule_id are also provided.
    """
    account_id  = request.GET.get('account_id', '').strip()
    channel     = request.GET.get('channel', '').strip()
    month       = request.GET.get('month', '').strip()
    schedule_id = request.GET.get('schedule_id', '').strip()
    if not account_id or not _account_access(request.user, account_id):
        return JsonResponse({'brands': [], 'themes': [], 'tc_themes': []})

    # ── Brands (from Schedule) ──────────────────────────────────────────────
    brands_qs = ScheduleRow.objects.filter(account_id=account_id).exclude(brand='')
    if schedule_id:
        brands_qs = brands_qs.filter(schedule_id=schedule_id)
    elif channel and month:
        brands_qs = brands_qs.filter(schedule__channel=channel, schedule__month=month)
    elif channel:
        brands_qs = brands_qs.filter(schedule__channel=channel)
    brands = sorted(set(brands_qs.values_list('brand', flat=True)))

    # ── Schedule date range for LMRB / TC filtering ─────────────────────────
    sch_filter = {'account_id': account_id}
    if schedule_id:
        sch_filter['id'] = schedule_id
    elif channel and month:
        sch_filter['channel'] = channel
        sch_filter['month']   = month
    elif channel:
        sch_filter['channel'] = channel
    sch_dates = Schedule.objects.filter(**sch_filter).aggregate(s=Min('start_date'), e=Max('end_date'))

    # ── LMRB themes ────────────────────────────────────────────────────────
    themes_qs = LMRBRow.objects.filter(account_id=account_id).exclude(advt_theme='')
    if channel:
        themes_qs = themes_qs.filter(channel__iexact=channel)
    if sch_dates['s']:
        themes_qs = themes_qs.filter(date__gte=sch_dates['s'])
    if sch_dates['e']:
        themes_qs = themes_qs.filter(date__lte=sch_dates['e'])
    themes = sorted(set(themes_qs.values_list('advt_theme', flat=True)))

    # ── TC themes ─────────────────────────────────────────────────────────
    tc_qs = TCRow.objects.filter(account_id=account_id).exclude(tc_theme='')
    if channel:
        tc_qs = tc_qs.filter(channel__iexact=channel)
    if sch_dates['s']:
        tc_qs = tc_qs.filter(date__gte=sch_dates['s'])
    if sch_dates['e']:
        tc_qs = tc_qs.filter(date__lte=sch_dates['e'])
    tc_themes = sorted(set(tc_qs.values_list('tc_theme', flat=True)))

    return JsonResponse({'brands': brands, 'themes': themes, 'tc_themes': tc_themes})


@login_required
def brand_mapping_channels(request):
    """AJAX: Return distinct channels from Schedules for an account."""
    account_id = request.GET.get('account_id', '').strip()
    if not account_id or not _account_access(request.user, account_id):
        return JsonResponse({'channels': []})
    channels = sorted(set(
        Schedule.objects.filter(account_id=account_id).values_list('channel', flat=True)
    ))
    return JsonResponse({'channels': channels})


@login_required
def brand_mapping_months(request):
    """AJAX: Return distinct months from Schedules for an account + channel."""
    account_id = request.GET.get('account_id', '').strip()
    channel    = request.GET.get('channel', '').strip()
    if not account_id or not _account_access(request.user, account_id):
        return JsonResponse({'months': []})
    months = sorted(set(
        Schedule.objects.filter(account_id=account_id, channel=channel)
        .values_list('month', flat=True)
    ))
    return JsonResponse({'months': months})


@login_required
def brand_mapping_schedules(request):
    """AJAX: Return schedules for an account + channel + month."""
    account_id = request.GET.get('account_id', '').strip()
    channel    = request.GET.get('channel', '').strip()
    month      = request.GET.get('month', '').strip()
    if not account_id or not _account_access(request.user, account_id):
        return JsonResponse({'schedules': []})
    schedules = list(
        Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
        .order_by('version')
        .values('id', 'schedule_number', 'version', 'row_count')
    )
    return JsonResponse({'schedules': schedules})


# ── Monitoring Dashboard (Items 3 + 4) ───────────────────────────────────────

@login_required
def monitoring_dashboard(request):
    """
    Analytics dashboard: per-scope summary, 7 report tabs, export + PDF links.
    Accessible by operations, team_head, planner, admin, super_admin.
    """
    # MatchResult and ScheduleRow already imported at top level

    user       = request.user
    account_qs = _account_qs(user)

    account_id  = request.GET.get('account_id', '')
    channel     = request.GET.get('channel', '')
    month       = request.GET.get('month', '')
    schedule_id = request.GET.get('schedule_id', '').strip()

    selected_account   = None
    channels           = []
    months             = []
    schedules_in_scope = []
    stats    = {}
    tab_data = {}
    ch_summary     = []
    brand_summary  = []
    sponsorship_rows = []
    sponsorship_brand_summary = []
    schedule_chart = []
    lmrb_chart     = []
    lmrb_matched_rows   = []
    lmrb_unmatched_rows = []

    if account_id:
        try:
            selected_account = account_qs.get(pk=account_id)
        except Account.DoesNotExist:
            pass

    if selected_account:
        channels = sorted(set(
            MatchResult.objects.filter(account_id=account_id)
            .values_list('channel', flat=True)
        ))
        if channel:
            months = sorted(set(
                MatchResult.objects.filter(account_id=account_id, channel=channel)
                .values_list('month', flat=True)
            ))

    if selected_account and channel and month:
        schedules_in_scope = list(
            Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
            .order_by('schedule_number')
        )
        if not schedule_id and len(schedules_in_scope) == 1:
            schedule_id = str(schedules_in_scope[0].id)

        qs = MatchResult.objects.filter(account_id=account_id, channel=channel, month=month)
        if schedule_id:
            qs = qs.filter(schedule_row__schedule_id=schedule_id)
        total = qs.count()

        n_matched    = qs.filter(status='matched').count()
        n_prog_mis   = qs.filter(status='programme_mismatch').count()
        n_late       = qs.filter(status='late_telecast').count()
        n_not_aired  = qs.filter(status__in=['not_aired', 'no_mapping']).count()
        n_no_map     = qs.filter(status='no_mapping').count()
        n_aired      = n_matched + n_prog_mis + n_late

        # Use active schedules only (highest version per schedule_number) so
        # duplicate uploads don't inflate commercial / sponsorship counts.
        from verification.engine import active_schedule_ids as _active_ids
        _active_sch_ids = _active_ids(account_id, channel, month)
        _sr_base = ScheduleRow.objects.filter(schedule_id__in=_active_sch_ids)
        if schedule_id and int(schedule_id) in _active_sch_ids:
            _sr_base = _sr_base.filter(schedule_id=schedule_id)
        n_sponsorship = _sr_base.filter(ad_type='SPONSORSHIP').count()
        n_commercial  = _sr_base.filter(ad_type='COMMERCIAL BENEFITS').count()

        # Compliance is against the planned commercial rows, not just the verified
        # subset — rows past the LMRB date cap are counted as unverified (not ignored).
        # Programme Mismatch and Late Telecast both count as "aired" for compliance.
        compliance = round((n_matched + n_prog_mis + n_late) / n_commercial * 100, 1) if n_commercial else 0

        stats = {
            'total':        n_commercial,                  # planned commercial rows = real denominator
            'total_all':    n_commercial + n_sponsorship,  # combined for split-bar denominator
            'verified':     total,                         # MatchResult count
            'unverified':   max(0, n_commercial - total),  # rows past LMRB date cap
            'matched':      n_matched,
            'prog_mis':     n_prog_mis,
            'late':         n_late,
            'not_aired':    n_not_aired,
            'no_map':       n_no_map,
            'aired':        n_aired,
            'compliance':   compliance,
            'sponsorship':  n_sponsorship,
        }

        tab_data = {
            'full':     list(qs.select_related('lmrb_row').order_by('scheduled_date', 'brand')),
            # Programme mismatch = valid match (same day, brand, theme — time offset only)
            'matched':  list(qs.filter(status__in=['matched', 'programme_mismatch']).select_related('lmrb_row').order_by('scheduled_date', 'brand')),
            'not_aired': list(qs.filter(status__in=['not_aired', 'no_mapping']).order_by('scheduled_date', 'brand')),
            'prog_mis': list(qs.filter(status='programme_mismatch').select_related('lmrb_row').order_by('scheduled_date', 'brand')),
            'late':     list(qs.filter(status='late_telecast').select_related('lmrb_row').order_by('scheduled_date', 'brand')),
        }

        # Channel summary (only one channel in this scope)
        ch_summary = [{'channel': channel, **stats}]

        # Brand summary
        for br in qs.values_list('brand', flat=True).distinct().order_by('brand'):
            bq       = qs.filter(brand=br)
            bt       = bq.count()
            bm       = bq.filter(status='matched').count()
            brand_summary.append({
                'brand':      br,
                'total':      bt,
                'matched':    bm,
                'prog_mis':   bq.filter(status='programme_mismatch').count(),
                'late':       bq.filter(status='late_telecast').count(),
                'not_aired':  bq.filter(status__in=['not_aired', 'no_mapping']).count(),
                'compliance': round(bm / bt * 100, 1) if bt else 0,
            })

        # Sponsorship rows for the separate sponsorship tab (active schedules only)
        sponsorship_rows = list(
            _sr_base.filter(ad_type='SPONSORSHIP').order_by('date', 'start_time')
        )

        # Sponsorship brand breakdown (active schedules only)
        sponsorship_brand_summary = []
        for br in (_sr_base.filter(ad_type='SPONSORSHIP')
                   .values_list('brand', flat=True).distinct().order_by('brand')):
            sq = _sr_base.filter(ad_type='SPONSORSHIP', brand=br)
            st = sq.count()
            sm = sq.filter(is_matched=True).count()
            sponsorship_brand_summary.append({
                'brand': br,
                'total': st,
                'matched': sm,
                'not_matched': st - sm,
            })

        # commercial count already computed above; expose it in stats
        stats['commercial'] = n_commercial

        # ── Chart data 1: Schedule spots grouped by Brand × Duration ─────────
        sch_rows = (
            ScheduleRow.objects
            .filter(account_id=account_id, channel=channel, month=month)
            .values('brand', 'duration')
            .annotate(count=Count('id'))
            .order_by('brand', 'duration')
        )
        schedule_chart = list(sch_rows)

        # ── Chart data 2: LMRB rows grouped by Theme × Duration ──────────────
        sch_dates = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month
        ).aggregate(d_min=Min('date'), d_max=Max('date'))
        lmrb_qs = LMRBRow.objects.filter(account_id=account_id, channel__iexact=channel)
        if sch_dates['d_min']:
            lmrb_qs = lmrb_qs.filter(date__gte=sch_dates['d_min'])
        if sch_dates['d_max']:
            lmrb_qs = lmrb_qs.filter(date__lte=sch_dates['d_max'])
        lmrb_chart = list(
            lmrb_qs.values('advt_theme', 'duration')
            .annotate(count=Count('id'))
            .order_by('advt_theme', 'duration')
        )

        # ── LMRB theme detail (per theme, for detail drawer) ─────────────────
        def _time_bucket(t):
            try:
                h = int(str(t).split(':')[0])
                if 19 <= h <= 23:
                    return 'prime'
                elif 6 <= h <= 18:
                    return 'non_prime'
                else:
                    return 'other'
            except Exception:
                return 'other'

        lmrb_theme_detail = []
        for td in lmrb_chart:
            t_theme = td['advt_theme']
            t_dur   = td['duration']
            tqs     = lmrb_qs.filter(advt_theme=t_theme, duration=t_dur)
            progs   = list(
                tqs.exclude(program='').exclude(program__isnull=True)
                .values('program').annotate(cnt=Count('id')).order_by('-cnt')
            )
            dates     = tqs.aggregate(first=Min('date'), last=Max('date'))
            date_span = (
                (dates['last'] - dates['first']).days + 1
                if (dates['first'] and dates['last']) else 1
            )
            avg_per_day = round(td['count'] / date_span, 1) if date_span else td['count']
            bm      = BrandMapping.objects.filter(
                account_id=account_id, theme__iexact=t_theme
            ).first()
            sch_start = None
            if bm:
                sch_start = ScheduleRow.objects.filter(
                    account_id=account_id, channel=channel, month=month,
                    brand=bm.brand,
                ).aggregate(d=Min('date'))['d']
            # PT split for this theme+duration
            t_prime = t_non = 0
            for r in tqs.values_list('advt_time', flat=True):
                b = _time_bucket(r)
                if b == 'prime':
                    t_prime += 1
                else:
                    t_non += 1
            # Daily airings count
            daily = list(
                tqs.values('date').annotate(cnt=Count('id')).order_by('date')
            )
            lmrb_theme_detail.append({
                'theme':       t_theme,
                'duration':    t_dur,
                'count':       td['count'],
                'programmes':  progs,
                'first_aired': dates['first'].isoformat() if dates['first'] else None,
                'last_aired':  dates['last'].isoformat()  if dates['last']  else None,
                'sch_start':   sch_start.isoformat()      if sch_start      else None,
                'avg_per_day': avg_per_day,
                'prime':       t_prime,
                'non_prime':   t_non,
                'prime_pct':   round(t_prime / td['count'] * 100) if td['count'] else 0,
                'daily':       [{'date': str(r['date']), 'cnt': r['cnt']} for r in daily],
            })

        # ── Prime-time distribution (planned vs LMRB) ────────────────────────
        # Build theme → ad_type lookup via BrandMapping
        commercial_brands   = set(ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
            ad_type='COMMERCIAL BENEFITS',
        ).values_list('brand', flat=True))
        sponsorship_brands  = set(ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
            ad_type='SPONSORSHIP',
        ).values_list('brand', flat=True))
        theme_to_adtype = {}
        for bm in BrandMapping.objects.filter(account_id=account_id):
            key = (bm.theme or '').lower().strip()
            if bm.brand in commercial_brands:
                theme_to_adtype.setdefault(key, 'commercial')
            if bm.brand in sponsorship_brands:
                theme_to_adtype[key] = 'sponsorship'
        # Load sponsorship keyword list from settings (e.g. -BB, Tag, Com Break …)
        from .models import get_setting_list as _gsl
        spon_kw = _gsl('lmrb_sponsorship_keywords')

        def _pcts(d):
            total = d['prime'] + d['non_prime'] + d['other']
            return {
                **d,
                'total':          total,
                'prime_pct':      round(d['prime']     / total * 100) if total else 0,
                'non_prime_pct':  round(d['non_prime'] / total * 100) if total else 0,
                'other_pct':      round(d['other']     / total * 100) if total else 0,
            }

        # LMRB prime time distribution
        _pt_all  = {'prime': 0, 'non_prime': 0, 'other': 0}
        _pt_comm = {'prime': 0, 'non_prime': 0, 'other': 0}
        _pt_spon = {'prime': 0, 'non_prime': 0, 'other': 0}
        for lr in lmrb_qs.values('advt_theme', 'advt_time'):
            bucket = _time_bucket(lr['advt_time'])
            _pt_all[bucket] += 1
            tp = _theme_adtype(lr['advt_theme'], theme_to_adtype, spon_kw)
            if tp == 'commercial':
                _pt_comm[bucket] += 1
            elif tp == 'sponsorship':
                _pt_spon[bucket] += 1
        pt_all  = _pcts(_pt_all)
        pt_comm = _pcts(_pt_comm)
        pt_spon = _pcts(_pt_spon)

        # Schedule (planned) prime time distribution
        _sch_pt_comm = {'prime': 0, 'non_prime': 0, 'other': 0}
        _sch_pt_spon = {'prime': 0, 'non_prime': 0, 'other': 0}
        for sr in ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).values('ad_type', 'start_time'):
            bucket = _time_bucket(sr['start_time'])
            if sr['ad_type'] == 'COMMERCIAL BENEFITS':
                _sch_pt_comm[bucket] += 1
            elif sr['ad_type'] == 'SPONSORSHIP':
                _sch_pt_spon[bucket] += 1
        sch_pt_comm = _pcts(_sch_pt_comm)
        sch_pt_spon = _pcts(_sch_pt_spon)

        # ── Matched LMRB rows (for LMRB Matched tab) ─────────────────────────
        lmrb_matched_rows = list(
            lmrb_qs.filter(is_matched=True).order_by('date', 'advt_time')
        )
        # ── Unmatched LMRB rows (for LMRB Unmatched tab) ─────────────────────
        lmrb_unmatched_rows = list(
            lmrb_qs.filter(is_matched=False).order_by('date', 'advt_time')
        )

        # ── Sponsorship Keyword-Type Breakdown ────────────────────────────────
        # For each keyword in lmrb_sponsorship_keywords, count LMRB rows that
        # contain that keyword (case-insensitive) grouped by PT / Non-PT, and
        # list the top programmes they aired in.
        spon_kw_breakdown = []
        if spon_kw:
            for kw in spon_kw:
                if not kw:
                    continue
                kw_qs = lmrb_qs.filter(advt_theme__icontains=kw)
                kw_total = kw_qs.count()
                if kw_total == 0:
                    continue
                kw_pt = kw_non_pt = 0
                for r in kw_qs.values('advt_time'):
                    b = _time_bucket(r['advt_time'])
                    if b == 'prime':
                        kw_pt += 1
                    elif b == 'non_prime':
                        kw_non_pt += 1
                progs = list(
                    kw_qs.exclude(program='').exclude(program__isnull=True)
                    .values('program').annotate(cnt=Count('id')).order_by('-cnt')[:8]
                )
                spon_kw_breakdown.append({
                    'kw':      kw,
                    'total':   kw_total,
                    'pt':      kw_pt,
                    'non_pt':  kw_non_pt,
                    'progs':   progs,
                })

        # Planned sponsorship PT/Non-PT for comparison
        spon_planned_pt     = _sch_pt_spon.get('prime', 0)
        spon_planned_non_pt = _sch_pt_spon.get('non_prime', 0)

        spon_kw_breakdown_json = _to_js(spon_kw_breakdown)

        # ── Advt_Theme Analysis tab data ──────────────────────────────────────
        # For each unique Advt_Theme: daily airings, programme breakdown, PT split
        theme_analysis = []
        all_themes = sorted(set(lmrb_qs.values_list('advt_theme', flat=True)))
        for t in all_themes:
            t_qs = lmrb_qs.filter(advt_theme=t)
            t_total = t_qs.count()
            # Daily counts
            daily = list(
                t_qs.values('date').annotate(cnt=Count('id')).order_by('date')
            )
            # Programme breakdown
            progs = list(
                t_qs.exclude(program='').values('program').annotate(cnt=Count('id')).order_by('-cnt')[:10]
            )
            # PT split
            t_prime = t_non = 0
            for r in t_qs.values_list('advt_time', flat=True):
                bucket = _time_bucket(r)
                if bucket == 'prime':
                    t_prime += 1
                else:
                    t_non += 1
            theme_analysis.append({
                'theme':    t,
                'total':    t_total,
                'prime':    t_prime,
                'non_prime': t_non,
                'prime_pct': round(t_prime / t_total * 100) if t_total else 0,
                'daily':    daily,
                'programmes': progs,
            })
        theme_analysis_json = _to_js(theme_analysis)

    else:
        theme_analysis = []
        theme_analysis_json = '[]'
        spon_kw_breakdown = []
        spon_kw_breakdown_json = '[]'
        spon_planned_pt = 0
        spon_planned_non_pt = 0

    return render(request, 'monitoring/dashboard.html', {
        'accounts':                  account_qs,
        'selected_account':          selected_account,
        'account_id':                account_id,
        'channels':                  channels,
        'months':                    months,
        'schedules_in_scope':        schedules_in_scope,
        'schedule_id':               schedule_id,
        'channel':                   channel,
        'month':                     month,
        'stats':                     stats,
        'tab_data':                  tab_data,
        'ch_summary':                ch_summary,
        'brand_summary':             brand_summary,
        'sponsorship_rows':          sponsorship_rows,
        'sponsorship_brand_summary': sponsorship_brand_summary,
        'schedule_chart':            schedule_chart,
        'lmrb_chart':                lmrb_chart,
        # Pre-serialised JSON — safe to emit with |safe in templates (no Python None/True/False)
        'schedule_chart_json':       _to_js(list(schedule_chart)),
        'lmrb_chart_json':           _to_js(list(lmrb_chart)),
        'lmrb_theme_detail':         lmrb_theme_detail if selected_account and channel and month else [],
        'lmrb_theme_detail_json':    _to_js(lmrb_theme_detail if selected_account and channel and month else []),
        'pt_all':                    pt_all if selected_account and channel and month else {},
        'pt_comm':                   pt_comm if selected_account and channel and month else {},
        'pt_spon':                   pt_spon if selected_account and channel and month else {},
        'sch_pt_comm':               sch_pt_comm if selected_account and channel and month else {},
        'sch_pt_spon':               sch_pt_spon if selected_account and channel and month else {},
        'lmrb_matched_rows':         lmrb_matched_rows,
        'lmrb_unmatched_rows':       lmrb_unmatched_rows,
        'theme_analysis':            theme_analysis,
        'theme_analysis_json':       theme_analysis_json,
        'spon_kw_breakdown':         spon_kw_breakdown if selected_account and channel and month else [],
        'spon_kw_breakdown_json':    spon_kw_breakdown_json if selected_account and channel and month else '[]',
        'spon_planned_pt':           spon_planned_pt if selected_account and channel and month else 0,
        'spon_planned_non_pt':       spon_planned_non_pt if selected_account and channel and month else 0,
    })


# ── Full Analytics Page ───────────────────────────────────────────────────────

@login_required
def analytics_full(request):
    """
    Comprehensive LMRB analytics page.
    Builds raw row JSON + pre-aggregated data for 25 charts across 6 sections.
    All chart computation + filtering is done client-side in JS using Chart.js.
    """
    from django.db.models import Avg, Sum

    user       = request.user
    account_qs = _account_qs(user)

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')
    # Prime-time window hours (configurable)
    prime_start_h = int(request.GET.get('prime_start', 18))
    prime_end_h   = int(request.GET.get('prime_end',   22))

    channels = []
    months   = []
    rows_json = '[]'
    kpis = {'total': 0, 'prime': 0, 'nonprime': 0, 'spon': 0, 'total_cost': None}
    filter_pg_list  = []
    filter_adv_list = []
    selected_account = None
    d_min = d_max = None

    if account_id and _account_access(user, account_id):
        try:
            selected_account = account_qs.get(id=account_id)
        except Exception:
            selected_account = None
        channels = list(
            ScheduleRow.objects.filter(account_id=account_id)
            .values_list('channel', flat=True).distinct().order_by('channel')
        )

    if account_id and channel and _account_access(user, account_id):
        months = list(
            ScheduleRow.objects.filter(account_id=account_id, channel=channel)
            .values_list('month', flat=True).distinct().order_by('month')
        )

    if account_id and channel and month and _account_access(user, account_id):
        # Date range from schedule
        dr = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month
        ).aggregate(d_min=Min('date'), d_max=Max('date'))
        d_min, d_max = dr['d_min'], dr['d_max']

        # Build theme → ad_type mapping
        commercial_brands  = set(ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
            ad_type='COMMERCIAL BENEFITS',
        ).values_list('brand', flat=True))
        sponsorship_brands = set(ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
            ad_type='SPONSORSHIP',
        ).values_list('brand', flat=True))
        theme_to_adtype = {}
        for bm in BrandMapping.objects.filter(account_id=account_id):
            key = (bm.theme or '').lower().strip()
            if bm.brand in commercial_brands:
                theme_to_adtype.setdefault(key, 'commercial')
            if bm.brand in sponsorship_brands:
                theme_to_adtype[key] = 'sponsorship'
        from .models import get_setting_list as _gsl
        spon_kw = _gsl('lmrb_sponsorship_keywords')

        # Fetch all LMRB rows for the scope
        lmrb_qs = LMRBRow.objects.filter(
            account_id=account_id, channel__iexact=channel,
        )
        if d_min:
            lmrb_qs = lmrb_qs.filter(date__gte=d_min)
        if d_max:
            lmrb_qs = lmrb_qs.filter(date__lte=d_max)

        # Build compact row list for JS
        raw = []
        total_cost_sum = 0.0
        has_cost = False
        for row in lmrb_qs.values(
            'id', 'date', 'advt_theme', 'advt_time', 'duration',
            'program', 'product_group', 'advertiser',
            'ad_pos', 'tot_ads', 'brk_no', 'pos_in_brk', 'ads_in_brk',
            'cost', 'day',
        ):
            try:
                h = int(str(row['advt_time'] or '0').split(':')[0])
            except Exception:
                h = 0
            adtype = _theme_adtype(row['advt_theme'], theme_to_adtype, spon_kw)
            is_p   = prime_start_h <= h < prime_end_h
            cost_v = float(row['cost']) if row['cost'] is not None else None
            if cost_v is not None:
                has_cost = True
                total_cost_sum += cost_v
            # day-of-week: use 'day' field if present, else derive from date
            dow_name = (row['day'] or '').strip()
            if not dow_name and row['date']:
                dow_name = row['date'].strftime('%a')
            raw.append({
                'd':        str(row['date']),
                'dow':      row['date'].weekday() if row['date'] else 0,
                'downame':  dow_name,
                'h':        h,
                'theme':    row['advt_theme'] or '',
                'dur':      row['duration'],
                'prog':     row['program'] or '',
                'pg':       row['product_group'] or '',
                'adv':      row['advertiser'] or '',
                'adtype':   adtype,
                'prime':    is_p,
                'adpos':    row['ad_pos'],
                'totads':   row['tot_ads'],
                'brkno':    row['brk_no'],
                'posinbrk': row['pos_in_brk'],
                'adsinbrk': row['ads_in_brk'],
                'cost':     cost_v,
            })

        rows_json = _to_js(raw)

        # KPI summary
        kpis = {
            'total':      len(raw),
            'prime':      sum(1 for r in raw if r['prime']),
            'nonprime':   sum(1 for r in raw if not r['prime']),
            'spon':       sum(1 for r in raw if r['adtype'] == 'sponsorship'),
            'total_cost': round(total_cost_sum, 2) if has_cost else None,
        }
        if kpis['total']:
            kpis['prime_pct'] = round(kpis['prime'] / kpis['total'] * 100)
        else:
            kpis['prime_pct'] = 0

        # Filter option lists for the UI dropdowns
        filter_pg_list  = sorted({r['pg']  for r in raw if r['pg']})
        filter_adv_list = sorted({r['adv'] for r in raw if r['adv']})[:100]

    return render(request, 'monitoring/analytics.html', {
        'accounts':         account_qs,
        'selected_account': selected_account,
        'account_id':       account_id,
        'channels':         channels,
        'months':           months,
        'channel':          channel,
        'month':            month,
        'prime_start_h':    prime_start_h,
        'prime_end_h':      prime_end_h,
        'rows_json':        rows_json,
        'kpis':             kpis,
        'filter_pg_list':   filter_pg_list,
        'filter_adv_list':  filter_adv_list,
        'd_min':            d_min,
        'd_max':            d_max,
    })


# ── PDF Missed-Ad Report (Item 5) ─────────────────────────────────────────────

@login_required
def monitoring_pdf(request):
    """
    Generate a professional ReportLab PDF of Not-Aired / Programme-Mismatch
    rows for a given scope (account + channel + month).
    """
    # MatchResult already imported at top level

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')

    if not (account_id and channel and month):
        return HttpResponse('Missing parameters: account_id, channel, month', status=400)
    if not _account_access(request.user, account_id):
        return HttpResponse('Access denied', status=403)

    try:
        account = Account.objects.get(pk=account_id)
    except Account.DoesNotExist:
        return HttpResponse('Account not found', status=404)

    missed_qs = MatchResult.objects.filter(
        account_id=account_id, channel=channel, month=month,
        status__in=['not_aired', 'no_mapping', 'programme_mismatch', 'late_telecast'],
    ).order_by('scheduled_date', 'brand')

    sch_numbers = list(
        Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
        .order_by('version')
        .values_list('schedule_number', flat=True)
    )
    schedule_number = ' / '.join(str(s) for s in sch_numbers if s)

    try:
        pdf_bytes = _build_missed_ad_pdf(
            account.name, channel, month, list(missed_qs),
            schedule_number=schedule_number,
        )
    except Exception as e:
        return HttpResponse(f'PDF generation failed: {e}', status=500)

    fname = f'missed_ads_{channel}_{month}.pdf'.replace(' ', '_')
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    return response


def _build_missed_ad_pdf(account_name, channel, month, rows, schedule_number=''):
    """Build a professional PDF bytes object using ReportLab."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable,
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT

    buf    = io.BytesIO()
    doc    = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=1.5*cm, rightMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm,
    )

    styles = getSampleStyleSheet()
    NAVY   = colors.HexColor('#0b1726')
    BLUE   = colors.HexColor('#2563eb')
    LGRAY  = colors.HexColor('#f8fafc')
    MGRAY  = colors.HexColor('#e2e8f0')
    RED    = colors.HexColor('#dc2626')
    AMBER  = colors.HexColor('#d97706')
    VIOLET = colors.HexColor('#7c3aed')

    STATUS_COLOR = {
        'Not Aired':           RED,
        'No Brand Mapping':    colors.HexColor('#64748b'),
        'Programme Mismatch':  AMBER,
        'Late Telecast':       VIOLET,
    }

    h_title = ParagraphStyle('title', fontSize=16, textColor=NAVY,
                              fontName='Helvetica-Bold', spaceAfter=4)
    h_sub   = ParagraphStyle('sub',   fontSize=9,  textColor=colors.HexColor('#475569'),
                              fontName='Helvetica', spaceAfter=2)
    h_cell  = ParagraphStyle('cell',  fontSize=7.5, fontName='Helvetica')
    h_hdr   = ParagraphStyle('hdr',   fontSize=7.5, fontName='Helvetica-Bold',
                              textColor=colors.white)

    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    story.append(Paragraph(f'Missed Ad Report — {channel}', h_title))
    meta_parts = [f'Account: {account_name}']
    if schedule_number:
        meta_parts.append(f'Schedule No: {schedule_number}')
    meta_parts.append(f'Month: {month}')
    story.append(Paragraph('  |  '.join(meta_parts), h_sub))
    story.append(Paragraph(
        f'Generated: {datetime.now().strftime("%d %B %Y %H:%M")}  |  '
        f'Total rows: {len(rows)}',
        h_sub,
    ))
    story.append(HRFlowable(width='100%', thickness=1, color=BLUE, spaceAfter=10))

    if not rows:
        story.append(Paragraph('No missed ads found for this scope.', styles['Normal']))
        doc.build(story)
        return buf.getvalue()

    # ── Table ─────────────────────────────────────────────────────────────────
    col_headers = [
        'Brand', 'Duration (s)', 'Programme',
        'Planned Date', 'Planned Start', 'Planned End',
        'Aired Date', 'Aired Time', 'Status',
    ]
    table_data = [[Paragraph(h, h_hdr) for h in col_headers]]

    for mr in rows:
        status_label = dict(mr.STATUS_CHOICES).get(mr.status, mr.status)
        table_data.append([
            Paragraph(mr.brand or '—', h_cell),
            Paragraph(str(mr.duration or '—'), h_cell),
            Paragraph(mr.programme or '—', h_cell),
            Paragraph(str(mr.scheduled_date or '—'), h_cell),
            Paragraph(mr.planned_start or '—', h_cell),
            Paragraph(mr.planned_end or '—', h_cell),
            Paragraph(str(mr.aired_date or '—'), h_cell),
            Paragraph(mr.air_time or '—', h_cell),
            Paragraph(status_label, ParagraphStyle(
                'st', fontSize=7.5, fontName='Helvetica-Bold',
                textColor=STATUS_COLOR.get(status_label, colors.black),
            )),
        ])

    # Column widths for landscape A4 (≈ 25 cm usable)
    col_widths = [4*cm, 1.8*cm, 4*cm, 2.2*cm, 2*cm, 2*cm, 2.2*cm, 2*cm, 3*cm]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        # Header row
        ('BACKGROUND',  (0, 0), (-1, 0),  NAVY),
        ('TEXTCOLOR',   (0, 0), (-1, 0),  colors.white),
        ('FONTNAME',    (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',    (0, 0), (-1, 0),  7.5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [LGRAY, colors.white]),
        ('FONTSIZE',    (0, 1), (-1, -1), 7.5),
        ('VALIGN',      (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',  (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ('GRID',        (0, 0), (-1, -1), 0.4, MGRAY),
        ('LINEBELOW',   (0, 0), (-1, 0),  1,   BLUE),
    ]))

    story.append(t)
    doc.build(story)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════════════
# TC (Transmission Certificate) Upload & Management
# ═══════════════════════════════════════════════════════════════════════════════

def _find_col(df, *names):
    """Case-insensitive column finder. Returns the actual column name in df, or None."""
    lower_map = {c.lower().strip(): c for c in df.columns}
    for n in names:
        actual = lower_map.get(n.lower().strip())
        if actual is not None:
            return actual
    return None


def _detect_tc_meta(df):
    """
    Auto-detect channel, date range and row count from a TC DataFrame.
    Returns {'channel': str, 'start_date': date, 'end_date': date, 'row_count': int}
    """
    channel_col = _find_col(df, 'Channel', 'Station', 'CHANNEL')
    channel = _safe_str(df[channel_col].dropna().iloc[0]) if channel_col else ''

    date_col = _find_col(df, 'Date', 'Aired Date', 'Prg Date', 'AiredDate')

    dates = []
    if date_col:
        for v in df[date_col].dropna():
            d = _safe_date(v)
            if d:
                dates.append(d)

    start_date = min(dates) if dates else None
    end_date   = max(dates) if dates else None
    return {
        'channel':    channel,
        'start_date': start_date,
        'end_date':   end_date,
        'row_count':  len(df),
    }


def _parse_tc_rows(df, account, tc_report):
    """
    Parse TC DataFrame and upsert TCRow records.
    Handles various column naming conventions (case-insensitive).
    Returns count of rows inserted.
    """
    # ── Case-insensitive column normalisation ─────────────────────────────────
    # Build a rename map: actual_col → standard_name (only when needed)
    rename = {}

    def _ci_rename(standard, alts):
        """Map first found alt (case-insensitive) to standard name."""
        if _find_col(df, standard) is not None:
            return  # already present
        actual = _find_col(df, *alts)
        if actual is not None:
            rename[actual] = standard

    # Super-admin can add extra column aliases via Settings page
    _tc_ex_theme = get_setting_list('tc_extra_theme_aliases')
    _tc_ex_time  = get_setting_list('tc_extra_time_aliases')
    _tc_ex_date  = get_setting_list('tc_extra_date_aliases')
    _tc_ex_dur   = get_setting_list('tc_extra_duration_aliases')
    _tc_ex_prog  = get_setting_list('tc_extra_programme_aliases')

    _ci_rename('Channel',   ['Station', 'CHANNEL', 'channel'])
    _ci_rename('Date',      ['Aired Date', 'Prg Date', 'aired_date', 'AiredDate', 'Prg_Date',
                              *_tc_ex_date])
    _ci_rename('Programme', ['Program', 'Prg Name', 'PrgName', 'programme', *_tc_ex_prog])
    _ci_rename('TC_Theme',  ['Advt_Theme', 'Advt_theme', 'Theme', 'theme',
                              'Product', 'Description', 'Ad Name', 'AdName', 'Ad_Name',
                              *_tc_ex_theme])
    _ci_rename('Duration',  ['Dur', 'Seconds', 'Ad Dur', 'Duration_Sec', *_tc_ex_dur])
    _ci_rename('Aired_Time',['Advt_Time', 'Advt_time', 'advt_Time', 'Time',
                              'Aired Time', 'Ad Start', 'AdTime', 'AiredTime', *_tc_ex_time])

    if rename:
        df = df.rename(columns=rename)

    # ── Fallback defaults ─────────────────────────────────────────────────────
    if _find_col(df, 'TC_Theme') is None:
        df['TC_Theme'] = ''
    if _find_col(df, 'Aired_Time') is None:
        df['Aired_Time'] = ''
    if _find_col(df, 'Duration') is None:
        df['Duration'] = None
    if _find_col(df, 'Programme') is None:
        df['Programme'] = ''
    if _find_col(df, 'Channel') is None:
        df['Channel'] = tc_report.channel

    channel = tc_report.channel

    rows_by_key = {}
    for _, r in df.iterrows():
        theme      = _safe_str(r.get('TC_Theme', ''))
        aired_time = _safe_str(r.get('Aired_Time', ''))
        dur        = _safe_int(r.get('Duration'))
        date_val   = _safe_date(r.get('Date'))

        if not (theme and aired_time and date_val):
            continue

        dedup_key = TCRow.make_dedup_key(account.id, channel, date_val, aired_time, theme, dur)
        rows_by_key[dedup_key] = TCRow(
            account    = account,
            tc_report  = tc_report,
            channel    = channel,
            date       = date_val,
            programme  = _safe_str(r.get('Programme', '')),
            tc_theme   = theme,
            duration   = dur,
            aired_time = aired_time,
            dedup_key  = dedup_key,
        )

    if not rows_by_key:
        return 0

    # Delete any existing rows with same dedup keys (re-upload replaces)
    existing_keys = list(rows_by_key.keys())
    TCRow.objects.filter(dedup_key__in=existing_keys).delete()

    new_rows = list(rows_by_key.values())
    TCRow.objects.bulk_create(new_rows, batch_size=500)
    return len(new_rows)


@login_required
@require_POST
def tc_preview(request):
    """
    AJAX: Parse a TC file and return a data summary for user review.
    Does NOT save anything to the database.
    """
    account_id = request.POST.get('account_id', '').strip()
    channel    = request.POST.get('channel', '').strip()
    tc_file    = request.FILES.get('tc_file')

    if not tc_file:
        return JsonResponse({'ok': False, 'error': 'No file provided.'})

    if not _account_access(request.user, account_id):
        return JsonResponse({'ok': False, 'error': 'Access denied.'})

    # ── Parse the file (PDF or Excel) ─────────────────────────────────────────
    filename_lower = tc_file.name.lower()
    is_pdf = filename_lower.endswith('.pdf')

    if is_pdf:
        from verification.tc_converters.dispatch import get_converter
        converter = get_converter(channel)
        import tempfile, os
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
                for chunk in tc_file.chunks():
                    tmp.write(chunk)
                tmp_path = tmp.name
            df = converter.parse_pdf(tmp_path)
        except Exception as e:
            return JsonResponse({'ok': False, 'error': f'Could not parse PDF: {e}'})
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        if df.empty:
            return JsonResponse({'ok': False, 'error': 'No rows could be extracted from the PDF.'})
        df['Channel'] = channel
        parse_method = 'pdf'
    else:
        try:
            df = pd.read_excel(tc_file, header=0)
        except Exception as e:
            return JsonResponse({'ok': False, 'error': f'Could not read file: {e}'})
        parse_method = 'excel'

    # ── Normalise column names (same logic as _parse_tc_rows) ─────────────────
    rename = {}

    def _ci_rename_local(standard, alts):
        if _find_col(df, standard) is not None:
            return
        actual = _find_col(df, *alts)
        if actual is not None:
            rename[actual] = standard

    _tc_ex_theme = get_setting_list('tc_extra_theme_aliases')
    _tc_ex_time  = get_setting_list('tc_extra_time_aliases')
    _tc_ex_date  = get_setting_list('tc_extra_date_aliases')
    _tc_ex_dur   = get_setting_list('tc_extra_duration_aliases')
    _tc_ex_prog  = get_setting_list('tc_extra_programme_aliases')

    _ci_rename_local('Channel',    ['Station', 'CHANNEL', 'channel'])
    _ci_rename_local('Date',       ['Aired Date', 'Prg Date', 'aired_date', 'AiredDate', 'Prg_Date', *_tc_ex_date])
    _ci_rename_local('Programme',  ['Program', 'Prg Name', 'PrgName', 'programme', *_tc_ex_prog])
    _ci_rename_local('TC_Theme',   ['Advt_Theme', 'Advt_theme', 'Theme', 'theme',
                                    'Product', 'Description', 'Ad Name', 'AdName', 'Ad_Name', *_tc_ex_theme])
    _ci_rename_local('Duration',   ['Dur', 'Seconds', 'Ad Dur', 'Duration_Sec', *_tc_ex_dur])
    _ci_rename_local('Aired_Time', ['Advt_Time', 'Advt_time', 'advt_Time', 'Time',
                                    'Aired Time', 'Ad Start', 'AdTime', 'AiredTime', *_tc_ex_time])
    if rename:
        df = df.rename(columns=rename)

    # Ensure required columns exist
    for col, default in [('TC_Theme', ''), ('Aired_Time', ''), ('Duration', None), ('Programme', '')]:
        if _find_col(df, col) is None:
            df[col] = default

    # ── Collect valid rows (same filters as _parse_tc_rows) ───────────────────
    rows = []
    skipped = 0
    for _, r in df.iterrows():
        theme      = _safe_str(r.get('TC_Theme', ''))
        aired_time = _safe_str(r.get('Aired_Time', ''))
        dur        = _safe_int(r.get('Duration'))
        date_val   = _safe_date(r.get('Date'))
        if not (theme and aired_time and date_val):
            skipped += 1
            continue
        rows.append({
            'date':      str(date_val),
            'programme': _safe_str(r.get('Programme', '')),
            'time':      aired_time,
            'theme':     theme,
            'duration':  dur,
        })

    if not rows:
        return JsonResponse({
            'ok': False,
            'error': f'No valid rows found (all {skipped} rows were missing Theme, Time, or Date). '
                     f'Check that column names are recognised.',
        })

    # ── Build summary ──────────────────────────────────────────────────────────
    from collections import Counter, defaultdict
    dates   = [r['date'] for r in rows]
    themes  = Counter()
    by_theme_dur = defaultdict(Counter)   # theme → {dur: count}

    for r in rows:
        key = (r['theme'], r['duration'])
        themes[key] += 1
        by_theme_dur[r['theme']][r['duration']] += 1

    theme_summary = []
    for (theme, dur), cnt in sorted(themes.items(), key=lambda x: -x[1]):
        theme_summary.append({'theme': theme, 'duration': dur, 'count': cnt})

    dur_breakdown = Counter(r['duration'] for r in rows)

    return JsonResponse({
        'ok':            True,
        'parse_method':  parse_method,
        'row_count':     len(rows),
        'skipped':       skipped,
        'date_range':    f"{min(dates)} → {max(dates)}",
        'themes':        theme_summary,
        'dur_breakdown': [{'duration': d, 'count': c}
                          for d, c in sorted(dur_breakdown.items(), key=lambda x: -(x[1]))],
        'sample_rows':   rows[:10],
    })


@login_required
@require_POST
def tc_upload_parsed(request):
    """
    Accept pre-parsed TC rows from the client-side PDF.js converter and save them.
    Rows arrive as a JSON array in POST['rows_json'].
    Each row: {date: "YYYY-MM-DD", programme, time, theme, duration}
    """
    account_id  = request.POST.get('account_id', '').strip()
    channel     = request.POST.get('channel', '').strip()
    month       = request.POST.get('month', '').strip()
    schedule_id = request.POST.get('schedule_id', '').strip()
    filename    = request.POST.get('filename', 'tc_upload.pdf')
    rows_json   = request.POST.get('rows_json', '[]')

    if not (account_id and channel and month):
        messages.error(request, 'Account, Channel and Month are required.')
        return redirect('/dashboard/tc/upload/')

    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/tc/upload/')

    try:
        raw_rows = json.loads(rows_json)
    except Exception:
        messages.error(request, 'Invalid row data received.')
        return redirect('/dashboard/tc/upload/')

    if not raw_rows:
        messages.error(request, 'No rows to upload.')
        return redirect('/dashboard/tc/upload/')

    account = get_object_or_404(Account, id=account_id)

    schedule_obj = None
    if schedule_id:
        try:
            schedule_obj = Schedule.objects.get(id=schedule_id, account=account)
        except Schedule.DoesNotExist:
            pass

    # Build DataFrame from the pre-parsed JSON rows
    records = []
    for r in raw_rows:
        date_val = _safe_date(r.get('date'))
        if date_val is None:
            continue
        records.append({
            'Date':       date_val,
            'Channel':    channel,
            'Programme':  str(r.get('programme') or ''),
            'Aired_Time': str(r.get('time') or ''),
            'TC_Theme':   str(r.get('theme') or ''),
            'Duration':   _safe_int(r.get('duration')),
        })

    if not records:
        messages.error(request, 'No valid rows found — all rows were missing a valid date.')
        return redirect('/dashboard/tc/upload/')

    df = pd.DataFrame(records)
    meta = _detect_tc_meta(df)

    tc_report = TransmissionReport.objects.create(
        account           = account,
        channel           = channel,
        month             = month,
        schedule          = schedule_obj,
        original_filename = filename,
        start_date        = meta.get('start_date'),
        end_date          = meta.get('end_date'),
        uploaded_by       = request.user,
    )

    rows_count = _parse_tc_rows(df, account, tc_report)
    tc_report.row_count = rows_count
    tc_report.save()

    messages.success(
        request,
        f'TC Report uploaded successfully: {rows_count} rows from "{filename}".'
    )
    return redirect('/dashboard/tc/')


@login_required
def tc_list(request):
    user       = request.user
    account_qs = _account_qs(user)
    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')

    reports = TransmissionReport.objects.filter(
        account__in=account_qs
    ).select_related('account', 'uploaded_by').order_by('-uploaded_at')

    if account_id:
        reports = reports.filter(account_id=account_id)
    if channel:
        reports = reports.filter(channel=channel)

    channels = sorted(set(
        TransmissionReport.objects.filter(account__in=account_qs)
        .values_list('channel', flat=True)
    ))

    return render(request, 'tc/list.html', {
        'reports':    reports,
        'accounts':   account_qs,
        'channels':   channels,
        'account_id': account_id,
        'channel':    channel,
    })


@login_required
@role_required(['super_admin', 'admin', 'operations'])
def tc_pdf_convert(request):
    """
    GET  — render the PDF TC converter page.
    POST — receive converted rows as JSON, save to DB, return summary.
    """
    user       = request.user
    account_qs = _account_qs(user)

    if request.method == 'POST':
        import json as _json
        from datetime import datetime as _dt

        try:
            body       = _json.loads(request.body)
            account_id = body.get('account_id', '').strip()
            channel    = body.get('channel', '').strip()
            rows       = body.get('rows', [])

            if not (account_id and channel and rows):
                return JsonResponse({'ok': False, 'error': 'Missing account, channel, or rows.'})

            account = Account.objects.get(pk=account_id)
            if not _account_access(user, account_id):
                return JsonResponse({'ok': False, 'error': 'Access denied.'})

            # Parse dates and derive month / date range
            from datetime import date as _date_t
            valid_dates = []
            parsed_rows = []
            for r in rows:
                raw_date = r.get('DATE', '')
                try:
                    # Converter outputs D/M/YYYY
                    parts = raw_date.split('/')
                    d = _date_t(int(parts[2]), int(parts[1]), int(parts[0]))
                    valid_dates.append(d)
                    parsed_rows.append({**r, '_date': d})
                except Exception:
                    continue

            if not parsed_rows:
                return JsonResponse({'ok': False, 'error': 'No valid rows with parseable dates.'})

            start_date = min(valid_dates)
            end_date   = max(valid_dates)
            month      = start_date.strftime('%B %Y')

            tc_report = TransmissionReport.objects.create(
                account           = account,
                channel           = channel,
                month             = month,
                file              = None,
                original_filename = f'PDF_Converted_{channel}_{month}.xlsx',
                row_count         = 0,
                start_date        = start_date,
                end_date          = end_date,
                uploaded_by       = user,
            )

            new_rows = []
            for r in parsed_rows:
                theme      = (r.get('Advt_theme') or '').strip()
                aired_time = (r.get('Advt_time')  or '').strip()
                dur        = _safe_int(r.get('Duration'))
                date_val   = r['_date']
                if not (theme and aired_time):
                    continue
                dedup_key = TCRow.make_dedup_key(account.id, channel, date_val, aired_time, theme, dur)
                new_rows.append(TCRow(
                    account    = account,
                    tc_report  = tc_report,
                    channel    = channel,
                    date       = date_val,
                    programme  = (r.get('PROGRAMME') or '').strip(),
                    tc_theme   = theme,
                    duration   = dur,
                    aired_time = aired_time,
                    dedup_key  = dedup_key,
                ))

            # Dedup + save
            existing_keys = [rw.dedup_key for rw in new_rows]
            TCRow.objects.filter(dedup_key__in=existing_keys).delete()
            TCRow.objects.bulk_create(new_rows, batch_size=500)
            tc_report.row_count = len(new_rows)
            tc_report.save(update_fields=['row_count'])

            # Summary: group by theme × duration
            from collections import Counter
            summary = {}
            for rw in new_rows:
                key = (rw.tc_theme, rw.duration)
                summary[key] = summary.get(key, 0) + 1
            summary_list = [
                {'theme': k[0], 'duration': k[1], 'count': v}
                for k, v in sorted(summary.items(), key=lambda x: -x[1])
            ]

            return JsonResponse({
                'ok':         True,
                'report_id':  tc_report.id,
                'month':      month,
                'row_count':  len(new_rows),
                'summary':    summary_list,
            })

        except Account.DoesNotExist:
            return JsonResponse({'ok': False, 'error': 'Account not found.'})
        except Exception as e:
            return JsonResponse({'ok': False, 'error': str(e)})

    return render(request, 'tc/pdf_convert.html', {
        'accounts': account_qs,
    })


@login_required
def tc_upload(request):
    user       = request.user
    account_qs = _account_qs(user)

    if request.method == 'POST':
        account_id = request.POST.get('account_id', '').strip()
        channel    = request.POST.get('channel', '').strip()
        month      = request.POST.get('month', '').strip()
        tc_file    = request.FILES.get('tc_file')
        schedule_id = request.POST.get('schedule_id', '').strip()

        if not (account_id and channel and month and tc_file):
            messages.error(request, 'Account, Channel, Month and TC file are required.')
            return redirect('/dashboard/tc/upload/')

        if not _account_access(user, account_id):
            messages.error(request, 'Access denied.')
            return redirect('/dashboard/tc/upload/')

        account = get_object_or_404(Account, id=account_id)

        # ── Detect file type and parse accordingly ────────────────────────────
        filename_lower = tc_file.name.lower()
        is_pdf = filename_lower.endswith('.pdf')

        if is_pdf:
            from verification.tc_converters.dispatch import get_converter
            converter = get_converter(channel)
            if converter is None:
                messages.error(
                    request,
                    f'No PDF converter is configured for channel "{channel}". '
                    f'Please upload an Excel file instead, or contact your administrator.'
                )
                return redirect('/dashboard/tc/upload/')

            import tempfile, os
            try:
                suffix = '.pdf'
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    for chunk in tc_file.chunks():
                        tmp.write(chunk)
                    tmp_path = tmp.name
                df = converter.parse_pdf(tmp_path)
            except Exception as e:
                messages.error(request, f'Could not parse PDF TC file: {e}')
                return redirect('/dashboard/tc/upload/')
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

            if df.empty:
                messages.error(request, 'No data rows could be extracted from the PDF.')
                return redirect('/dashboard/tc/upload/')

            # PDF parser already returns standard column names; ensure Channel present
            df['Channel'] = channel
        else:
            try:
                df = pd.read_excel(tc_file, header=0)
            except Exception as e:
                messages.error(request, f'Could not read TC file: {e}')
                return redirect('/dashboard/tc/upload/')

        schedule_obj = None
        if schedule_id:
            try:
                schedule_obj = Schedule.objects.get(id=schedule_id, account=account)
            except Schedule.DoesNotExist:
                pass

        meta = _detect_tc_meta(df)

        tc_report = TransmissionReport.objects.create(
            account           = account,
            channel           = channel,
            month             = month,
            schedule          = schedule_obj,
            file              = tc_file,
            original_filename = tc_file.name,
            row_count         = 0,
            start_date        = meta['start_date'],
            end_date          = meta['end_date'],
            uploaded_by       = user,
        )

        count = _parse_tc_rows(df, account, tc_report)
        tc_report.row_count = count
        tc_report.save(update_fields=['row_count'])

        messages.success(request, f'TC uploaded: {count} rows for {channel} / {month}.')
        return redirect('/dashboard/tc/')

    # GET — show upload form
    schedules = Schedule.objects.filter(account__in=account_qs).select_related('account').order_by('-uploaded_at')
    return render(request, 'tc/upload.html', {
        'accounts':  account_qs,
        'schedules': schedules,
    })


@login_required
def tc_detect(request):
    """AJAX: Detect channel + dates from an uploaded TC file."""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'error': 'POST required'})

    tc_file = request.FILES.get('tc_file')
    if not tc_file:
        return JsonResponse({'ok': False, 'error': 'No file'})

    # PDF files: we can't detect metadata at AJAX time without a full parse;
    # return ok=True with empty fields so the form keeps what the user typed.
    if tc_file.name.lower().endswith('.pdf'):
        return JsonResponse({
            'ok':         True,
            'channel':    '',
            'start_date': '',
            'end_date':   '',
            'row_count':  0,
            'columns':    [],
            'is_pdf':     True,
        })

    try:
        df   = pd.read_excel(tc_file, header=0)
        meta = _detect_tc_meta(df)
        return JsonResponse({
            'ok':         True,
            'channel':    meta['channel'],
            'start_date': str(meta['start_date']) if meta['start_date'] else '',
            'end_date':   str(meta['end_date'])   if meta['end_date']   else '',
            'row_count':  meta['row_count'],
            'columns':    list(df.columns[:20]),
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
@require_POST
def tc_delete(request, pk):
    report = get_object_or_404(TransmissionReport, pk=pk)
    if not _account_access(request.user, report.account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/tc/')
    channel = report.channel
    month   = report.month
    report.delete()
    messages.success(request, f'TC report for {channel} / {month} deleted.')
    return redirect('/dashboard/tc/')


@login_required
def tc_reconcile(request):
    """Run TC-Schedule + TC-LMRB reconciliation for a scope."""
    from verification.tc_engine import reconcile_tc

    account_id  = request.POST.get('account_id') or request.GET.get('account_id', '')
    channel     = request.POST.get('channel')    or request.GET.get('channel', '')
    month       = request.POST.get('month')      or request.GET.get('month', '')
    mode        = request.POST.get('mode', 'smart')
    schedule_id = (request.POST.get('schedule_id') or request.GET.get('schedule_id', '')).strip()

    if not (account_id and channel and month):
        messages.error(request, 'Account, channel and month are required.')
        return redirect('/dashboard/tc/')

    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/tc/')

    sid = int(schedule_id) if schedule_id else None
    try:
        result = reconcile_tc(account_id, channel, month, mode=mode, schedule_id=sid)
        msg = (
            f'Reconciliation complete: {result["matched"]} matched, '
            f'{result["extra"]} extra, {result["lmrb_confirmed"]} LMRB-confirmed.'
        )
        messages.success(request, msg)

        # ── Diagnostic: if 0 TC-Schedule matches, show the unique TC themes so
        # the user knows what to enter in BrandMapping → tc_theme field.
        if result['matched'] == 0 and result['extra'] > 0:
            unique_themes = list(
                TCRow.objects.filter(
                    account_id=account_id, channel=channel, tc_report__month=month,
                ).values_list('tc_theme', flat=True).distinct().order_by('tc_theme')
            )
            # Also check if BrandMappings exist with tc_theme set
            mapped_themes = list(
                BrandMapping.objects.filter(account_id=account_id)
                .exclude(tc_theme='').values_list('tc_theme', flat=True).distinct()
            )
            if not mapped_themes:
                themes_str = ', '.join(f'"{t}"' for t in unique_themes[:10])
                messages.warning(
                    request,
                    f'No BrandMapping tc_theme values are configured. '
                    f'The TC file contains these themes: {themes_str}. '
                    f'Go to Brand Mappings and set the "TC Theme" field for each brand '
                    f'to match exactly what appears in the TC file.'
                )
    except Exception as e:
        messages.error(request, f'Reconciliation failed: {e}')

    redir = f'/dashboard/summary/?account_id={account_id}&channel={channel}&month={month}'
    if schedule_id:
        redir += f'&schedule_id={schedule_id}'
    return redirect(redir)


# ═══════════════════════════════════════════════════════════════════════════════
# TC Three-Way Comparison  (Plan vs LMRB vs TC)
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def tc_three_way(request):
    """
    Three-way evidence view for each planned ad in a scope:
      PLAN  — what was scheduled  (ScheduleRow)
      LMRB  — what 3rd-party monitoring observed  (matched LMRBRow)
      TC    — what the channel's own certificate says  (matched TCRow)

    Matching priority applied by the engines (already in DB):
      1. Theme  (via BrandMapping)
      2. Duration
      3. Date   (LMRB within schedule date range; TC late-aired rule: date >= schedule date)
      4. Advt_Time / Aired_Time (±5 sec tolerance for TC-LMRB cross-check)
    """
    user       = request.user
    account_qs = _account_qs(user)

    account_id  = request.GET.get('account_id', '')
    channel     = request.GET.get('channel', '')
    month       = request.GET.get('month', '')
    schedule_id = request.GET.get('schedule_id', '').strip()

    selected_account   = None
    channels           = []
    months             = []
    schedules_in_scope = []
    rows               = []

    if account_id:
        try:
            selected_account = account_qs.get(pk=account_id)
        except Account.DoesNotExist:
            pass

    if selected_account:
        channels = sorted(set(
            ScheduleRow.objects.filter(account_id=account_id)
            .values_list('channel', flat=True)
        ))
        if channel:
            months = sorted(set(
                ScheduleRow.objects.filter(account_id=account_id, channel=channel)
                .values_list('month', flat=True)
            ))

    if selected_account and channel and month:
        if not _account_access(user, account_id):
            messages.error(request, 'Access denied.')
            return redirect('/dashboard/tc/detail/')

        schedules_in_scope = list(
            Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
            .order_by('schedule_number')
        )
        if not schedule_id and len(schedules_in_scope) == 1:
            schedule_id = str(schedules_in_scope[0].id)

        from core.models import ManualMatch
        sch_filter = dict(account_id=account_id, channel=channel, month=month)
        if schedule_id:
            sch_filter['schedule_id'] = schedule_id
        sch_qs = (
            ScheduleRow.objects
            .filter(**sch_filter)
            .select_related('matched_lmrb')
            .prefetch_related('tc_matches', 'tc_matches__matched_lmrb')
            .order_by('date', 'start_time', 'brand')
        )

        # Build manual match lookup: {schedule_row_id: ManualMatch}
        mm_lookup = {
            mm.schedule_row_id: mm
            for mm in ManualMatch.objects.filter(
                account_id=account_id, channel=channel, month=month,
                schedule_row__isnull=False,
            ).select_related('tc_row', 'lmrb_row')
        }

        for sr in sch_qs:
            lmrb = sr.matched_lmrb
            tc   = sr.tc_matches.filter(is_schedule_matched=True).first()

            # Check for manual match (3way) which also sets is_schedule_matched
            mm = mm_lookup.get(sr.id)
            is_manual = mm is not None

            # If engine didn't find a TC match but there's a manual 3way link, use that
            if not tc and mm and mm.tc_row:
                tc = mm.tc_row

            # Determine row status for colour-coding
            if tc and tc.is_lmrb_confirmed:
                status = 'aired'          # confirmed by both TC and LMRB
            elif tc and not tc.is_lmrb_confirmed:
                status = 'tc_only'        # TC says aired but LMRB doesn't confirm
            elif lmrb or (mm and mm.lmrb_row):
                status = 'lmrb_only'      # LMRB found a match but no TC record
            elif is_manual:
                status = 'manual'         # manually matched, no TC/LMRB confirmation
            else:
                status = 'not_aired'      # neither source confirms it

            # Use manual LMRB if engine didn't lock one
            if not lmrb and mm and mm.lmrb_row:
                lmrb = mm.lmrb_row

            # ── Display LMRB: prefer the LMRB that TC was confirmed against ──────
            # When TC is confirmed, tc.matched_lmrb is the LMRB row within ±tolerance
            # seconds of the TC aired time — i.e. the SAME physical broadcast.
            # Using sr.matched_lmrb (Schedule↔LMRB engine result) can show a
            # different LMRB from a completely different time slot, which confuses
            # users into thinking the pair is wrong.  When TC is not confirmed we
            # fall back to sr.matched_lmrb so something useful is still shown.
            display_lmrb = lmrb
            if tc and tc.is_lmrb_confirmed and tc.matched_lmrb_id:
                display_lmrb = tc.matched_lmrb

            rows.append({
                'brand':          sr.brand,
                'ad_type':        sr.ad_type,
                'duration':       sr.duration,
                # Plan (Schedule)
                'plan_date':      sr.date,
                'plan_programme': sr.programme,
                'plan_start':     sr.start_time,
                'plan_end':       sr.end_time,
                # LMRB (3rd-party monitoring) — uses TC-confirmed LMRB when available
                'lmrb_date':      display_lmrb.date       if display_lmrb else None,
                'lmrb_programme': display_lmrb.program    if display_lmrb else '',
                'lmrb_time':      display_lmrb.advt_time  if display_lmrb else '',
                'lmrb_theme':     display_lmrb.advt_theme if display_lmrb else '',
                # TC (channel certificate)
                'tc_date':        tc.date          if tc else None,
                'tc_programme':   tc.programme     if tc else '',
                'tc_time':        tc.aired_time    if tc else '',
                'tc_theme':       tc.tc_theme      if tc else '',
                # Status flags
                'has_lmrb':            display_lmrb is not None,
                'has_tc':              tc is not None,
                'tc_lmrb_confirmed':   tc.is_lmrb_confirmed if tc else False,
                'is_manual':           is_manual,
                'status':              status,
            })

    return render(request, 'tc/detail.html', {
        'accounts':           account_qs,
        'selected_account':   selected_account,
        'account_id':         account_id,
        'channels':           channels,
        'months':             months,
        'schedules_in_scope': schedules_in_scope,
        'schedule_id':        schedule_id,
        'channel':            channel,
        'month':              month,
        'rows':               rows,
        'total':              len(rows),
        'n_aired':            sum(1 for r in rows if r['status'] == 'aired'),
        'n_tc_only':          sum(1 for r in rows if r['status'] == 'tc_only'),
        'n_lmrb_only':        sum(1 for r in rows if r['status'] == 'lmrb_only'),
        'n_not_aired':        sum(1 for r in rows if r['status'] == 'not_aired'),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Summary Sheet Report
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def summary_report(request):
    """View + edit Summary Sheet metadata; preview the reconciliation results."""
    from verification.tc_engine import build_summary_data

    user       = request.user
    account_qs = _account_qs(user)

    account_id  = request.GET.get('account_id', '')
    channel     = request.GET.get('channel', '')
    month       = request.GET.get('month', '')
    schedule_id = request.GET.get('schedule_id', '').strip()

    # Save metadata if POST
    if request.method == 'POST':
        account_id  = request.POST.get('account_id', '').strip()
        channel     = request.POST.get('channel', '').strip()
        month       = request.POST.get('month', '').strip()
        schedule_id = request.POST.get('schedule_id', '').strip()

        if account_id and channel and month and _account_access(user, account_id):
            account = get_object_or_404(Account, id=account_id)
            meta, _ = SummaryReportMeta.objects.get_or_create(
                account=account, channel=channel, month=month
            )
            meta.supplier_invoice_no = request.POST.get('supplier_invoice_no', '').strip()
            meta.po_no               = request.POST.get('po_no', '').strip()
            meta.invoice_no          = request.POST.get('invoice_no', '').strip()
            meta.notes               = request.POST.get('notes', '').strip()
            meta.prepared_by         = request.POST.get('prepared_by', '').strip()
            meta.checked_by          = request.POST.get('checked_by', '').strip()
            meta.authorised_by       = request.POST.get('authorised_by', '').strip()
            meta.save()
            messages.success(request, 'Summary metadata saved.')
        qs_str = f'account_id={account_id}&channel={channel}&month={month}'
        if schedule_id:
            qs_str += f'&schedule_id={schedule_id}'
        return redirect(f'/dashboard/summary/?{qs_str}')

    # Dropdown data
    channels          = []
    months            = []
    schedules_in_scope = []
    selected_account  = None
    summary_data      = None
    meta              = None
    schedule_obj      = None
    tc_report         = None

    if account_id:
        try:
            selected_account = account_qs.get(pk=account_id)
        except Account.DoesNotExist:
            pass

    if selected_account:
        channels = sorted(set(
            ScheduleRow.objects.filter(account_id=account_id)
            .values_list('channel', flat=True)
        ))
        if channel:
            months = sorted(set(
                ScheduleRow.objects.filter(account_id=account_id, channel=channel)
                .values_list('month', flat=True)
            ))

    if selected_account and channel and month:
        if not _account_access(user, account_id):
            messages.error(request, 'Access denied.')
            return redirect('/dashboard/summary/')

        # Build schedule selector for this scope
        schedules_in_scope = list(
            Schedule.objects.filter(account_id=account_id, channel=channel, month=month)
            .order_by('schedule_number')
        )
        # Auto-select when only one schedule exists
        if not schedule_id and len(schedules_in_scope) == 1:
            schedule_id = str(schedules_in_scope[0].id)

        sid = int(schedule_id) if schedule_id else None
        try:
            summary_data = build_summary_data(account_id, channel, month, schedule_id=sid)
        except Exception as e:
            messages.warning(request, f'Could not build summary: {e}')

        meta = SummaryReportMeta.objects.filter(
            account_id=account_id, channel=channel, month=month
        ).first()

        if schedule_id:
            schedule_obj = Schedule.objects.filter(id=schedule_id).first()
        else:
            schedule_obj = schedules_in_scope[0] if schedules_in_scope else None

        tc_report = TransmissionReport.objects.filter(
            account_id=account_id, channel=channel, month=month
        ).order_by('-uploaded_at').first()

    return render(request, 'summary/report.html', {
        'accounts':           account_qs,
        'selected_account':   selected_account,
        'account_id':         account_id,
        'channels':           channels,
        'months':             months,
        'schedules_in_scope': schedules_in_scope,
        'schedule_id':        schedule_id,
        'channel':            channel,
        'month':              month,
        'summary_data':       summary_data,
        'meta':               meta,
        'schedule_obj':       schedule_obj,
        'tc_report':          tc_report,
    })


@login_required
def summary_excel(request):
    """Download Summary Sheet as a formatted Excel workbook."""
    import openpyxl
    from openpyxl.styles import (
        Alignment, Border, Font, PatternFill, Side,
    )
    from openpyxl.utils import get_column_letter
    from verification.tc_engine import build_summary_data

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')

    if not (account_id and channel and month):
        messages.error(request, 'Incomplete parameters.')
        return redirect('/dashboard/summary/')

    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/summary/')

    account  = get_object_or_404(Account, id=account_id)
    data     = build_summary_data(account_id, channel, month)
    meta     = SummaryReportMeta.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).first()
    sched    = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).order_by('-uploaded_at').first()

    estimate_no = sched.schedule_number if sched else ''

    # ── Workbook setup ────────────────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Summary'

    # Styles
    NAVY  = PatternFill('solid', fgColor='0F2340')
    LBLUE = PatternFill('solid', fgColor='DBEAFE')
    LGRAY = PatternFill('solid', fgColor='F8FAFC')
    DGRAY = PatternFill('solid', fgColor='E2E8F0')
    GOLD  = PatternFill('solid', fgColor='FEF9C3')
    GRNFILL = PatternFill('solid', fgColor='DCFCE7')
    REDFILL  = PatternFill('solid', fgColor='FEE2E2')

    hdr_font  = Font(bold=True, color='FFFFFF', size=11)
    bold11    = Font(bold=True, size=11)
    bold10    = Font(bold=True, size=10)
    norm10    = Font(size=10)
    title_font = Font(bold=True, size=14, color='0F2340')

    thin = Side(style='thin', color='CBD5E1')
    thick_side = Side(style='medium', color='2563EB')
    def border(left=True, right=True, top=True, bottom=True):
        return Border(
            left=Side(style='thin', color='CBD5E1') if left else Side(),
            right=Side(style='thin', color='CBD5E1') if right else Side(),
            top=Side(style='thin', color='CBD5E1') if top else Side(),
            bottom=Side(style='thin', color='CBD5E1') if bottom else Side(),
        )

    centre = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left   = Alignment(horizontal='left',   vertical='center', wrap_text=True)
    right  = Alignment(horizontal='right',  vertical='center')

    # Column widths (7 data columns — Avg 30s removed)
    ws.column_dimensions['A'].width = 36
    ws.column_dimensions['B'].width = 8
    ws.column_dimensions['C'].width = 10
    ws.column_dimensions['D'].width = 10
    ws.column_dimensions['E'].width = 10
    ws.column_dimensions['F'].width = 10
    ws.column_dimensions['G'].width = 12

    row = 1

    # ── Title ─────────────────────────────────────────────────────────────────
    ws.merge_cells(f'A{row}:G{row}')
    c = ws.cell(row, 1, 'SUMMARY SHEET REPORT')
    c.font = title_font; c.alignment = centre; c.fill = LGRAY
    ws.row_dimensions[row].height = 28
    row += 1

    # ── Company info rows ─────────────────────────────────────────────────────
    company_info = [
        ('Phoenix O & M (Pvt) Ltd', 'MONTH',                month),
        ('No 16, Barnes Place',     'CHANNEL',               channel),
        ('Colombo 7',               'ESTIMATE NO',           estimate_no),
        ('',                        'SUPPLIER INVOICE NO',   meta.supplier_invoice_no if meta else ''),
        ('',                        'PO NO',                 meta.po_no if meta else ''),
        ('',                        'INVOICE NO',            meta.invoice_no if meta else ''),
    ]
    for addr, label, value in company_info:
        ws.merge_cells(f'A{row}:C{row}')
        c = ws.cell(row, 1, addr); c.font = norm10; c.alignment = left
        ws.merge_cells(f'D{row}:E{row}')
        c = ws.cell(row, 4, label + '  :'); c.font = bold10; c.alignment = right
        ws.merge_cells(f'F{row}:G{row}')
        c = ws.cell(row, 6, value); c.font = norm10; c.alignment = left
        row += 1

    row += 1  # blank

    # ── Column headers ────────────────────────────────────────────────────────
    headers = ['PRODUCT', 'DUR', 'PLANNED', 'AIRED', 'MISSED', 'EXTRA', '3RD PARTY']
    for col_i, h in enumerate(headers, start=1):
        c = ws.cell(row, col_i, h)
        c.font = hdr_font; c.fill = NAVY; c.alignment = centre
        c.border = border()
    ws.row_dimensions[row].height = 20
    row += 1

    # ── Commercial Benefits section ───────────────────────────────────────────
    if data['commercial']:
        ws.merge_cells(f'A{row}:G{row}')
        c = ws.cell(row, 1, 'Commercial Benefits'); c.font = bold10; c.fill = LBLUE
        c.alignment = left
        row += 1

        for item in data['commercial']:
            vals = [item['product'], item['dur'], item['planned'], item['aired'],
                    item['missed'], item['extra'], item['third_party']]
            fill = REDFILL if item['missed'] > 0 else None
            for col_i, v in enumerate(vals, start=1):
                c = ws.cell(row, col_i, v)
                c.font = norm10
                c.alignment = centre if col_i > 1 else left
                c.border = border()
                if fill:
                    c.fill = fill
            row += 1

        # Grand Total
        t = data['commercial_total']
        total_vals = ['Grand Total', '', t['planned'], t['aired'], t['missed'],
                      t['extra'], t['third_party']]
        for col_i, v in enumerate(total_vals, start=1):
            c = ws.cell(row, col_i, v)
            c.font = bold10; c.fill = DGRAY; c.alignment = centre if col_i > 1 else left
            c.border = border()
        row += 1

    row += 1  # blank

    # ── Sponsorship Benefits section ──────────────────────────────────────────
    if data['sponsorship']:
        ws.merge_cells(f'A{row}:G{row}')
        c = ws.cell(row, 1, 'Sponsorship Benefits'); c.font = bold11; c.fill = GOLD
        c.alignment = left
        row += 1

        for section in data['sponsorship']:
            # Programme heading
            ws.merge_cells(f'A{row}:G{row}')
            c = ws.cell(row, 1, section['programme'].upper())
            c.font = bold10; c.fill = LBLUE; c.alignment = left
            row += 1

            for item in section['rows']:
                vals = [item['product'], item['dur'], item['planned'], item['aired'],
                        item['missed'], item['extra'], item['third_party']]
                for col_i, v in enumerate(vals, start=1):
                    c = ws.cell(row, col_i, v)
                    c.font = norm10
                    c.alignment = centre if col_i > 1 else left
                    c.border = border()
                row += 1
            # No per-programme subtotal — only grand total at end

        # Sponsorship Grand Total
        st = data['sponsorship_total']
        ws.merge_cells(f'A{row}:G{row}')
        c = ws.cell(row, 1, 'SPONSORSHIP GRAND TOTAL'); c.font = bold11; c.fill = GOLD
        c.alignment = left
        row += 1
        total_vals = ['Grand Total', '', st['planned'], st['aired'], st['missed'],
                      st['extra'], st['third_party']]
        for col_i, v in enumerate(total_vals, start=1):
            c = ws.cell(row, col_i, v)
            c.font = bold10; c.fill = DGRAY; c.alignment = centre if col_i > 1 else left
            c.border = border()
        row += 1

    row += 1  # blank

    # ── Notes ─────────────────────────────────────────────────────────────────
    ws.merge_cells(f'A{row}:G{row}')
    ws.cell(row, 1, 'NOTE:').font = bold10
    row += 1
    notes_text = meta.notes if meta and meta.notes else 'Channel transmission attached\nSponsorship Benefits confirmation email attached'
    for line in notes_text.split('\n'):
        ws.merge_cells(f'A{row}:G{row}')
        c = ws.cell(row, 1, line.strip()); c.font = norm10
        row += 1

    row += 2  # blank

    # ── Signatures (3 across 7 columns: A-B, C-D-E, F-G) ─────────────────────
    sig_labels = ['PREPARED BY', 'CHECKED BY', 'AUTHORISED BY']
    sig_values = [
        meta.prepared_by if meta else '',
        meta.checked_by  if meta else '',
        meta.authorised_by if meta else '',
    ]
    sig_ranges = [(1, 2), (3, 5), (6, 7)]
    for (start_col, end_col), lbl in zip(sig_ranges, sig_labels):
        ws.merge_cells(
            start_row=row, start_column=start_col,
            end_row=row,   end_column=end_col,
        )
        c = ws.cell(row, start_col, '………………………………………………')
        c.font = norm10; c.alignment = centre
        ws.merge_cells(
            start_row=row+1, start_column=start_col,
            end_row=row+1,   end_column=end_col,
        )
        c = ws.cell(row+1, start_col, lbl); c.font = bold10; c.alignment = centre

    # ── Matched LMRB sheet (second sheet) ────────────────────────────────────
    ws_lmrb = wb.create_sheet('Matched LMRB')
    _write_matched_lmrb_sheet(ws_lmrb, account_id, channel, month)

    # ── Build response ────────────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f'Summary_{account.name}_{channel}_{month}.xlsx'.replace(' ', '_')
    resp  = HttpResponse(
        buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


@login_required
def summary_pdf(request):
    """Download professional Reconciliation PDF: Cover + Summary (portrait) + Matched LMRB (landscape)."""
    from reportlab.lib import colors
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4, landscape as rl_landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        BaseDocTemplate, Frame, PageTemplate, NextPageTemplate,
        Table, TableStyle, Paragraph, Spacer,
        HRFlowable, PageBreak,
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from verification.tc_engine import build_summary_data
    from django.db.models import Min, Max

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')

    if not (account_id and channel and month):
        messages.error(request, 'Incomplete parameters.')
        return redirect('/dashboard/summary/')

    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/summary/')

    account = get_object_or_404(Account, id=account_id)
    data    = build_summary_data(account_id, channel, month)
    meta    = SummaryReportMeta.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).first()
    sched   = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).order_by('-uploaded_at').first()
    estimate_no = sched.schedule_number if sched else ''

    # ── Colors ────────────────────────────────────────────────────────────────
    NAVY  = HexColor('#0f2340')
    BLUE  = HexColor('#2563eb')
    LGRAY = HexColor('#f8fafc')
    MGRAY = HexColor('#e2e8f0')
    DGRAY = HexColor('#e2e8f0')

    # ── Page sizes & margins ─────────────────────────────────────────────────
    PORT_W, PORT_H = A4
    LAND_W, LAND_H = rl_landscape(A4)
    MARGIN  = 1.5 * cm
    FOOT_H  = 1.0 * cm   # reserved at bottom for footer
    TOP_PAD = 2.0 * cm   # reserved at top for logo

    # ── Logo path ────────────────────────────────────────────────────────────
    logo_path = None
    logo_url  = _branding_url('logo')
    if logo_url:
        try:
            lp = os.path.join(
                django_settings.MEDIA_ROOT,
                logo_url.replace(django_settings.MEDIA_URL, '', 1),
            )
            if os.path.exists(lp):
                logo_path = lp
        except Exception:
            pass

    # ── Report metadata ───────────────────────────────────────────────────────
    report_ts   = datetime.now().strftime('%d %B %Y %H:%M')
    invoice_ref = ''
    if meta:
        invoice_ref = meta.invoice_no or meta.supplier_invoice_no or meta.po_no or ''

    # ── onPage callbacks — draw logo + footer on every page ──────────────────
    def _draw_page_chrome(canvas, doc, pw, ph):
        canvas.saveState()
        # Logo top-right
        if logo_path:
            lw, lh = 3.5 * cm, 1.4 * cm
            try:
                from reportlab.lib.utils import ImageReader as _IR
                _img = _IR(logo_path)
                canvas.drawImage(
                    _img,
                    pw - MARGIN - lw, ph - MARGIN - lh,
                    width=lw, height=lh,
                    preserveAspectRatio=True,
                )
            except Exception:
                pass
        # Footer separator line
        canvas.setStrokeColor(MGRAY)
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, FOOT_H, pw - MARGIN, FOOT_H)
        # Footer text
        canvas.setFont('Helvetica', 6.5)
        canvas.setFillColor(HexColor('#64748b'))
        left_txt = f'CONFIDENTIAL  \u2022  Generated: {report_ts}'
        if invoice_ref:
            left_txt += f'  \u2022  Ref: {invoice_ref}'
        canvas.drawString(MARGIN, FOOT_H * 0.38, left_txt)
        canvas.drawRightString(pw - MARGIN, FOOT_H * 0.38, f'Page {doc.page}')
        canvas.restoreState()

    def _portrait_page(canvas, doc):
        _draw_page_chrome(canvas, doc, PORT_W, PORT_H)

    def _landscape_page(canvas, doc):
        _draw_page_chrome(canvas, doc, LAND_W, LAND_H)

    # ── Frames ───────────────────────────────────────────────────────────────
    def _mk_frame(w, h, fid):
        return Frame(
            MARGIN,
            FOOT_H + 0.1 * cm,
            w - 2 * MARGIN,
            h - MARGIN - FOOT_H - 0.1 * cm - TOP_PAD,
            leftPadding=0, rightPadding=0,
            topPadding=0, bottomPadding=0,
            id=fid,
        )

    portrait_frame  = _mk_frame(PORT_W, PORT_H, 'portrait')
    landscape_frame = _mk_frame(LAND_W, LAND_H, 'landscape')

    # ── Document ─────────────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN + TOP_PAD, bottomMargin=FOOT_H + 0.2 * cm,
    )
    doc.addPageTemplates([
        PageTemplate(id='portrait',  frames=[portrait_frame],
                     onPage=_portrait_page, pagesize=A4),
        PageTemplate(id='landscape', frames=[landscape_frame],
                     onPage=_landscape_page, pagesize=rl_landscape(A4)),
    ])

    # ── Paragraph styles ─────────────────────────────────────────────────────
    def _ps(name, **kw):
        kw.setdefault('fontName', 'Helvetica')
        return ParagraphStyle(name, **kw)

    S = dict(
        title     = _ps('pdf_title',   fontSize=22, textColor=NAVY, fontName='Helvetica-Bold',
                        spaceAfter=4, alignment=TA_LEFT),
        sub_hdr   = _ps('pdf_subhdr',  fontSize=10, textColor=NAVY, fontName='Helvetica-Bold',
                        spaceAfter=2),
        sub       = _ps('pdf_sub',     fontSize=8.5, textColor=HexColor('#475569'), spaceAfter=2),
        sect      = _ps('pdf_sect',    fontSize=9,  textColor=NAVY, fontName='Helvetica-Bold',
                        spaceAfter=2),
        cell      = _ps('pdf_cell',    fontSize=7.5),
        cell_sm   = _ps('pdf_cellsm',  fontSize=5.5),
        hdr_wht   = _ps('pdf_hdrwht',  fontSize=7,  textColor=colors.white,
                        fontName='Helvetica-Bold'),
        cover_t   = _ps('pdf_covt',    fontSize=26, textColor=NAVY, fontName='Helvetica-Bold',
                        alignment=TA_CENTER, spaceAfter=6),
        cover_s   = _ps('pdf_covs',    fontSize=12, textColor=HexColor('#475569'),
                        alignment=TA_CENTER, spaceAfter=4),
        cover_lbl = _ps('pdf_covlbl',  fontSize=9,  textColor=HexColor('#64748b'),
                        fontName='Helvetica-Bold', alignment=TA_CENTER),
        cover_val = _ps('pdf_covval',  fontSize=12, textColor=NAVY,
                        fontName='Helvetica-Bold', alignment=TA_CENTER),
        conf      = _ps('pdf_conf',    fontSize=8,  textColor=HexColor('#94a3b8'),
                        fontName='Helvetica-Bold', alignment=TA_CENTER),
        prog_lbl  = _ps('pdf_proglbl', fontSize=8,  textColor=BLUE,
                        fontName='Helvetica-Bold'),
        gt        = _ps('pdf_gt',      fontSize=7.5, fontName='Helvetica-Bold'),
    )

    story = []

    # ═══════════════════════════════════════════════════════════════════════════
    # COVER PAGE (Portrait) — navy header band design
    # ═══════════════════════════════════════════════════════════════════════════

    from reportlab.platypus import Image as _RLImage
    from reportlab.lib.colors import HexColor as _HC

    # Cover-page colour palette
    C_NAVY   = _HC('#1F3864')
    C_ACCENT = _HC('#2E75B6')
    C_LGRAY  = _HC('#F2F2F2')
    C_WHITE  = colors.white

    content_w = PORT_W - 2 * MARGIN

    # Campaign period
    sch_dates = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).aggregate(d_min=Min('start_date'), d_max=Max('end_date'))
    date_min = sch_dates.get('d_min')
    date_max = sch_dates.get('d_max')
    campaign_period = ''
    if date_min and date_max:
        campaign_period = (
            f"{date_min.strftime('%d %b %Y')} \u2013 {date_max.strftime('%d %b %Y')}"
        )
    elif date_min:
        campaign_period = date_min.strftime('%d %b %Y')

    # Totals for KPI strip
    comm_total    = data.get('commercial_total', {})
    spon_total    = data.get('sponsorship_total', {})
    total_planned = (comm_total.get('planned', 0) or 0) + (spon_total.get('planned', 0) or 0)
    total_aired   = (comm_total.get('aired',   0) or 0) + (spon_total.get('aired',   0) or 0)
    total_missed  = (comm_total.get('missed',  0) or 0) + (spon_total.get('missed',  0) or 0)

    # ── Paragraph styles for cover page ──────────────────────────────────────
    def _cps(name, **kw):
        kw.setdefault('fontName', 'Helvetica')
        return ParagraphStyle(name, **kw)

    CS = dict(
        hdr_channel = _cps('cov_hdrch',  fontSize=9,  textColor=C_WHITE,
                            fontName='Helvetica', spaceAfter=4),
        hdr_title   = _cps('cov_hdrtit', fontSize=20, textColor=C_WHITE,
                            fontName='Helvetica-Bold', leading=24, spaceAfter=0),
        lbl         = _cps('cov_lbl',    fontSize=9,  textColor=HexColor('#475569'),
                            fontName='Helvetica-Bold', alignment=TA_LEFT),
        val         = _cps('cov_val',    fontSize=10, textColor=C_NAVY,
                            fontName='Helvetica-Bold', alignment=TA_LEFT),
        kpi_lbl     = _cps('cov_kpi_l',  fontSize=8,  textColor=HexColor('#64748b'),
                            fontName='Helvetica-Bold', alignment=TA_CENTER),
        kpi_val     = _cps('cov_kpi_v',  fontSize=22, textColor=C_NAVY,
                            fontName='Helvetica-Bold', alignment=TA_CENTER, spaceAfter=2),
        conf        = _cps('cov_conf',   fontSize=8,  textColor=HexColor('#94a3b8'),
                            fontName='Helvetica-Bold', alignment=TA_CENTER),
    )

    # ── Navy header band ─────────────────────────────────────────────────────
    # Left cell: subtitle + main title (white text)
    hdr_left_inner = Table(
        [
            [Paragraph('ADVERTISEMENT MONITORING', CS['hdr_channel'])],
            [Paragraph('RECONCILIATION<br/>SUMMARY REPORT', CS['hdr_title'])],
        ],
        colWidths=[content_w * 0.72],
    )
    hdr_left_inner.setStyle(TableStyle([
        ('TOPPADDING',    (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING',   (0, 0), (-1, -1), 0),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 0),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    # Right cell: logo or blank
    if logo_path:
        try:
            _logo_cov = _RLImage(logo_path, width=3.2 * cm, height=1.3 * cm, kind='bound')
        except Exception:
            _logo_cov = Spacer(3.2 * cm, 1.3 * cm)
    else:
        _logo_cov = Spacer(3.2 * cm, 1.3 * cm)

    hdr_tbl = Table(
        [[hdr_left_inner, _logo_cov]],
        colWidths=[content_w * 0.72, content_w * 0.28],
    )
    hdr_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, -1), C_NAVY),
        ('TOPPADDING',    (0, 0), (-1, -1), 20),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 20),
        ('LEFTPADDING',   (0, 0), (0, 0),  20),
        ('LEFTPADDING',   (1, 0), (1, 0),   8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 15),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN',         (1, 0), (1, 0),  'RIGHT'),
    ]))
    story.append(hdr_tbl)
    story.append(Spacer(1, 0.6 * cm))

    # ── Info block (two-column table) ─────────────────────────────────────────
    cover_meta = [
        ['Client / Account', account.name],
        ['Agency',           'Phoenix O & M (Pvt) Ltd'],
        ['Channel',          channel],
        ['Campaign Period',  campaign_period or '\u2014'],
        ['Estimate / RO No.', estimate_no or '\u2014'],
        ['Report Date',      report_ts[:11]],
    ]
    if meta:
        if meta.invoice_no:
            cover_meta.append(['Invoice No.',  meta.invoice_no])
        if meta.po_no:
            cover_meta.append(['Release Order Ref.', meta.po_no])

    cov_tbl = Table(
        [[Paragraph(k, CS['lbl']), Paragraph(str(v), CS['val'])]
         for k, v in cover_meta],
        colWidths=[5 * cm, 10 * cm],
        hAlign='LEFT',
    )
    cov_tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (0, -1), C_LGRAY),
        ('BACKGROUND',    (1, 0), (1, -1), C_WHITE),
        ('GRID',          (0, 0), (-1, -1), 0.5, HexColor('#cbd5e1')),
        ('TOPPADDING',    (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING',   (0, 0), (-1, -1), 10),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 10),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(cov_tbl)
    story.append(Spacer(1, 0.5 * cm))

    # ── Bold horizontal rule ──────────────────────────────────────────────────
    story.append(HRFlowable(width='100%', thickness=2.5, color=C_NAVY, spaceAfter=12))

    # ── KPI boxes: Total Planned | Total Aired | Total Missed ─────────────────
    kpi_items = [
        ('TOTAL PLANNED', str(total_planned)),
        ('TOTAL AIRED',   str(total_aired)),
        ('TOTAL MISSED',  str(total_missed)),
    ]
    kpi_cells = []
    for label, val in kpi_items:
        inner = Table(
            [
                [Paragraph(label, CS['kpi_lbl'])],
                [Paragraph(val,   CS['kpi_val'])],
            ],
            colWidths=[4.8 * cm],
        )
        inner.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), C_LGRAY),
            ('TOPPADDING',    (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING',   (0, 0), (-1, -1), 6),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
            ('ALIGN',         (0, 0), (-1, -1), 'CENTER'),
            ('LINEBELOW',     (0, 0), (-1, 0),  3, C_ACCENT),
        ]))
        kpi_cells.append(inner)
    kpi_row = Table([kpi_cells], colWidths=[5.2 * cm, 5.2 * cm, 5.2 * cm], hAlign='LEFT')
    kpi_row.setStyle(TableStyle([
        ('LEFTPADDING',  (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(kpi_row)
    story.append(Spacer(1, 0.8 * cm))

    # ── Signature fields (optional) ───────────────────────────────────────────
    if meta and any([meta.prepared_by, meta.checked_by, meta.authorised_by]):
        sig_tbl = Table(
            [[
                Paragraph(f'Prepared by<br/>{meta.prepared_by or ""}', CS['lbl']),
                Paragraph(f'Checked by<br/>{meta.checked_by or ""}',   CS['lbl']),
                Paragraph(f'Authorised by<br/>{meta.authorised_by or ""}', CS['lbl']),
            ]],
            colWidths=[5 * cm, 5 * cm, 5 * cm],
            hAlign='LEFT',
        )
        sig_tbl.setStyle(TableStyle([
            ('TOPPADDING',    (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
            ('LEFTPADDING',   (0, 0), (-1, -1), 6),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
            ('LINEABOVE',     (0, 0), (-1, 0),  0.5, HexColor('#94a3b8')),
        ]))
        story.append(sig_tbl)
        story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph('CONFIDENTIAL \u2014 For authorised recipients only', S['conf']))

    # ═══════════════════════════════════════════════════════════════════════════
    # SUMMARY TABLES (Portrait)
    # ═══════════════════════════════════════════════════════════════════════════
    story.append(PageBreak())

    story.append(Paragraph('RECONCILIATION SUMMARY', S['sub_hdr']))
    story.append(Paragraph(
        f'{account.name}  \u00b7  {channel}  \u00b7  {month}'
        + (f'  \u00b7  Estimate: {estimate_no}' if estimate_no else ''),
        S['sub'],
    ))
    story.append(HRFlowable(width='100%', thickness=1, color=BLUE, spaceAfter=8))

    # Summary table: 7 columns, portrait width ≈ 18cm
    SUM_HEADERS = ['Product / Brand', 'Dur', 'Planned', 'Aired', 'Missed', 'Extra', '3rd Party']
    SUM_WIDTHS  = [5.5 * cm, 1.5 * cm, 2.1 * cm, 2.1 * cm, 2.1 * cm, 2.1 * cm, 2.1 * cm]

    def _sum_style():
        return TableStyle([
            ('BACKGROUND',    (0, 0), (-1, 0),  NAVY),
            ('TEXTCOLOR',     (0, 0), (-1, 0),  colors.white),
            ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
            ('FONTSIZE',      (0, 0), (-1, -1), 7.5),
            ('ROWBACKGROUNDS',(0, 1), (-1, -2), [LGRAY, colors.white]),
            ('BACKGROUND',    (0, -1),(-1, -1), DGRAY),
            ('FONTNAME',      (0, -1),(-1, -1), 'Helvetica-Bold'),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',    (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING',   (0, 0), (-1, -1), 5),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
            ('GRID',          (0, 0), (-1, -1), 0.3, MGRAY),
            ('LINEBELOW',     (0, 0), (-1, 0),  1.2, BLUE),
        ])

    def _summary_table(rows, total_row):
        tdata = [[Paragraph(h, S['hdr_wht']) for h in SUM_HEADERS]]
        for r in rows:
            tdata.append([
                Paragraph(str(r.get('product', '')), S['cell']),
                Paragraph(str(r.get('dur') or '\u2014'), S['cell']),
                Paragraph(str(r.get('planned',     0)), S['cell']),
                Paragraph(str(r.get('aired',       0)), S['cell']),
                Paragraph(str(r.get('missed',      0)), S['cell']),
                Paragraph(str(r.get('extra',       0)), S['cell']),
                Paragraph(str(r.get('third_party', 0)), S['cell']),
            ])
        t = total_row
        tdata.append([
            Paragraph('Grand Total', S['gt']),
            Paragraph('', S['cell']),
            Paragraph(str(t.get('planned',     0)), S['cell']),
            Paragraph(str(t.get('aired',       0)), S['cell']),
            Paragraph(str(t.get('missed',      0)), S['cell']),
            Paragraph(str(t.get('extra',       0)), S['cell']),
            Paragraph(str(t.get('third_party', 0)), S['cell']),
        ])
        tbl = Table(tdata, colWidths=SUM_WIDTHS, repeatRows=1)
        tbl.setStyle(_sum_style())
        return tbl

    # Commercial Benefits
    if data.get('commercial'):
        story.append(Paragraph('Commercial Benefits', S['sect']))
        story.append(Spacer(1, 0.2 * cm))
        story.append(_summary_table(data['commercial'], data['commercial_total']))
        story.append(Spacer(1, 0.4 * cm))

    # Sponsorship Benefits — one table per programme, grand total at end
    if data.get('sponsorship'):
        story.append(Paragraph('Sponsorship Benefits', S['sect']))
        story.append(Spacer(1, 0.2 * cm))
        for section in data['sponsorship']:
            story.append(Paragraph(section['programme'].upper(), S['prog_lbl']))
            story.append(Spacer(1, 0.1 * cm))
            tdata_s = [[Paragraph(h, S['hdr_wht']) for h in SUM_HEADERS]]
            for r in section['rows']:
                tdata_s.append([
                    Paragraph(str(r.get('product', '')), S['cell']),
                    Paragraph(str(r.get('dur') or '\u2014'), S['cell']),
                    Paragraph(str(r.get('planned',     0)), S['cell']),
                    Paragraph(str(r.get('aired',       0)), S['cell']),
                    Paragraph(str(r.get('missed',      0)), S['cell']),
                    Paragraph(str(r.get('extra',       0)), S['cell']),
                    Paragraph(str(r.get('third_party', 0)), S['cell']),
                ])
            s_tbl = Table(tdata_s, colWidths=SUM_WIDTHS, repeatRows=1)
            s_tbl.setStyle(TableStyle([
                ('BACKGROUND',    (0, 0), (-1, 0),  NAVY),
                ('TEXTCOLOR',     (0, 0), (-1, 0),  colors.white),
                ('FONTSIZE',      (0, 0), (-1, -1), 7.5),
                ('ROWBACKGROUNDS',(0, 1), (-1, -1), [LGRAY, colors.white]),
                ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING',    (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING',   (0, 0), (-1, -1), 5),
                ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
                ('GRID',          (0, 0), (-1, -1), 0.3, MGRAY),
                ('LINEBELOW',     (0, 0), (-1, 0),  1.2, BLUE),
            ]))
            story.append(s_tbl)
            story.append(Spacer(1, 0.3 * cm))
        # Sponsorship Grand Total
        story.append(Paragraph('SPONSORSHIP GRAND TOTAL', S['sect']))
        story.append(Spacer(1, 0.1 * cm))
        story.append(_summary_table([], data['sponsorship_total']))

    # Notes
    if meta and meta.notes:
        story.append(Spacer(1, 0.4 * cm))
        story.append(Paragraph('Notes:', S['sect']))
        for line in meta.notes.splitlines():
            story.append(Paragraph(line, S['sub']))

    # ═══════════════════════════════════════════════════════════════════════════
    # MATCHED LMRB REPORT (Landscape)
    # ═══════════════════════════════════════════════════════════════════════════
    story.append(NextPageTemplate('landscape'))
    story.append(PageBreak())

    story.append(Paragraph('MATCHED LMRB REPORT', S['sub_hdr']))
    story.append(Paragraph(
        f'{account.name}  \u00b7  {channel}  \u00b7  {month}'
        '  \u00b7  Commercial (TC-confirmed) + Sponsorship (assigned)',
        S['sub'],
    ))
    story.append(HRFlowable(width='100%', thickness=1, color=BLUE, spaceAfter=8))

    # Fetch LMRB rows
    commercial_lmrb_qs = LMRBRow.objects.filter(account_id=account_id, channel__iexact=channel)
    if date_min and date_max:
        commercial_lmrb_qs = commercial_lmrb_qs.filter(date__range=(date_min, date_max))
    commercial_lmrb_qs = (
        commercial_lmrb_qs
        .filter(tc_confirmations__isnull=False)
        .distinct()
        .order_by('date', 'program', 'advt_time')
    )

    from core.models import SponsorshipLmrbAssignment
    spon_lmrb_ids = set(
        SponsorshipLmrbAssignment.objects.filter(
            account_id=account_id,
            schedule_row__channel=channel,
            schedule_row__month=month,
        ).values_list('lmrb_row_id', flat=True)
    )
    commercial_ids       = set(commercial_lmrb_qs.values_list('id', flat=True))
    sponsorship_only_ids = spon_lmrb_ids - commercial_ids
    sponsorship_only_qs  = (
        LMRBRow.objects.filter(id__in=sponsorship_only_ids)
        .order_by('date', 'program', 'advt_time')
    )
    all_lmrb_rows = list(commercial_lmrb_qs) + list(sponsorship_only_qs)

    LMRB_HEADERS = [
        'Product\nGroup', 'Advertiser', 'Product', 'Advt\nTheme', 'Ads',
        'Channel', 'Program',
        'Dd', 'Mn', 'Yr', 'Day', 'Prog\nTime', 'Advt\nTime',
        'AdPos', 'TotAds', 'BrkNo', 'PosIn\nBrk', 'AdsIn\nBrk',
        'Lng', 'Dur', 'Cost',
    ]
    LMRB_WIDTHS = [
        2.2 * cm, 2.5 * cm, 2.2 * cm, 3.0 * cm, 0.8 * cm,
        2.0 * cm, 2.5 * cm,
        0.65 * cm, 0.65 * cm, 0.9 * cm, 0.8 * cm, 1.2 * cm, 1.2 * cm,
        0.9 * cm, 0.9 * cm, 0.9 * cm, 0.9 * cm, 0.9 * cm,
        0.7 * cm, 0.7 * cm, 1.3 * cm,
    ]
    lmrb_tbl_style = TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0),  NAVY),
        ('TEXTCOLOR',     (0, 0), (-1, 0),  colors.white),
        ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [LGRAY, colors.white]),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING',   (0, 0), (-1, -1), 2),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 2),
        ('GRID',          (0, 0), (-1, -1), 0.3, MGRAY),
        ('LINEBELOW',     (0, 0), (-1, 0),  1.2, BLUE),
    ])

    if all_lmrb_rows:
        lmrb_data = [[Paragraph(h, S['hdr_wht']) for h in LMRB_HEADERS]]
        for lr in all_lmrb_rows:
            cost_str = f'{lr.cost:,.2f}' if lr.cost is not None else ''
            dd = str(lr.date.day)   if lr.date else ''
            mn = str(lr.date.month) if lr.date else ''
            yr = str(lr.date.year)  if lr.date else ''
            lmrb_data.append([
                Paragraph(str(lr.product_group or ''), S['cell_sm']),
                Paragraph(str(lr.advertiser or ''), S['cell_sm']),
                Paragraph(str(lr.product or ''), S['cell_sm']),
                Paragraph(str(lr.advt_theme or ''), S['cell_sm']),
                Paragraph(str(lr.ads or ''), S['cell_sm']),
                Paragraph(str(lr.channel or ''), S['cell_sm']),
                Paragraph(str(lr.program or ''), S['cell_sm']),
                Paragraph(dd, S['cell_sm']),
                Paragraph(mn, S['cell_sm']),
                Paragraph(yr, S['cell_sm']),
                Paragraph(str(lr.day or ''), S['cell_sm']),
                Paragraph(str(lr.prog_time or ''), S['cell_sm']),
                Paragraph(str(lr.advt_time or ''), S['cell_sm']),
                Paragraph(str(lr.ad_pos     if lr.ad_pos     is not None else ''), S['cell_sm']),
                Paragraph(str(lr.tot_ads    if lr.tot_ads    is not None else ''), S['cell_sm']),
                Paragraph(str(lr.brk_no     if lr.brk_no     is not None else ''), S['cell_sm']),
                Paragraph(str(lr.pos_in_brk if lr.pos_in_brk is not None else ''), S['cell_sm']),
                Paragraph(str(lr.ads_in_brk if lr.ads_in_brk is not None else ''), S['cell_sm']),
                Paragraph(str(lr.lng or ''), S['cell_sm']),
                Paragraph(str(lr.duration or ''), S['cell_sm']),
                Paragraph(cost_str, S['cell_sm']),
            ])
        lmrb_tbl = Table(lmrb_data, colWidths=LMRB_WIDTHS, repeatRows=1)
        lmrb_tbl.setStyle(lmrb_tbl_style)
        story.append(lmrb_tbl)
    else:
        story.append(Paragraph('No matched LMRB rows found for this scope.', S['sub']))

    # ── Build response ────────────────────────────────────────────────────────
    doc.build(story)
    buf.seek(0)
    fname = f'Reconciliation_{account.name}_{channel}_{month}.pdf'.replace(' ', '_')
    response = HttpResponse(buf.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    return response


def _write_matched_lmrb_sheet(ws, account_id, channel, month):
    """
    Shared helper: write all matched LMRB rows (commercial + sponsorship) into
    an existing openpyxl worksheet.  Returns the row count written.

    Commercial matched  = LMRBRow.is_matched=True (Schedule↔LMRB engine)
    Sponsorship matched = LMRBRow rows linked via SponsorshipLmrbAssignment
    Both sets are deduplicated so a row appearing in both groups is written once.
    """
    import openpyxl  # noqa — imported by callers too; ensure available
    from openpyxl.styles import Font, Alignment, PatternFill
    from django.db.models import Min, Max
    from core.models import SponsorshipLmrbAssignment

    sch_dates = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month,
    ).aggregate(d_min=Min('start_date'), d_max=Max('end_date'))
    date_min = sch_dates.get('d_min')
    date_max = sch_dates.get('d_max')

    # Commercial matched (auto-matched by Schedule↔LMRB engine)
    comm_qs = LMRBRow.objects.filter(
        account_id=account_id, channel__iexact=channel, is_matched=True,
    )
    if date_min and date_max:
        comm_qs = comm_qs.filter(date__range=(date_min, date_max))

    # Sponsorship matched (via SponsorshipLmrbAssignment)
    spon_ids = set(
        SponsorshipLmrbAssignment.objects.filter(
            account_id=account_id,
            schedule_row__channel=channel,
            schedule_row__month=month,
        ).values_list('lmrb_row_id', flat=True)
    )
    spon_qs = LMRBRow.objects.filter(id__in=spon_ids) if spon_ids else LMRBRow.objects.none()

    # Combine, deduplicate by id, sort by date+time
    all_ids  = set(comm_qs.values_list('id', flat=True)) | spon_ids
    combined = list(
        LMRBRow.objects.filter(id__in=all_ids).order_by('date', 'advt_time')
    )

    HDR_FILL = PatternFill('solid', fgColor='0F2340')
    hdr_font = Font(bold=True, color='FFFFFF', size=10)
    norm     = Font(size=10)
    centre   = Alignment(horizontal='center', vertical='center')

    headers = [
        'Product_Group', 'Advertiser', 'Product', 'Advt_Theme', 'Ads',
        'Channel', 'Program',
        'Dd', 'Mn', 'Yr', 'Day', 'Prog_time', 'Advt_time',
        'AdPos', 'TotAds', 'BrkNo', 'PosinBrk', 'AdsinBrk',
        'Lng', 'Dur', 'Cost',
    ]
    for col_i, h in enumerate(headers, start=1):
        c = ws.cell(1, col_i, h)
        c.font = hdr_font; c.fill = HDR_FILL; c.alignment = centre

    for row_i, lr in enumerate(combined, start=2):
        dd = lr.date.day   if lr.date else ''
        mn = lr.date.month if lr.date else ''
        yr = lr.date.year  if lr.date else ''
        vals = [
            lr.product_group or '',
            lr.advertiser or '',
            lr.product or '',
            lr.advt_theme or '',
            lr.ads or '',
            lr.channel or '',
            lr.program or '',
            dd, mn, yr,
            lr.day or '',
            lr.prog_time or '',
            lr.advt_time or '',
            lr.ad_pos     if lr.ad_pos     is not None else '',
            lr.tot_ads    if lr.tot_ads    is not None else '',
            lr.brk_no     if lr.brk_no     is not None else '',
            lr.pos_in_brk if lr.pos_in_brk is not None else '',
            lr.ads_in_brk if lr.ads_in_brk is not None else '',
            lr.lng or '',
            lr.duration   if lr.duration   is not None else '',
            float(lr.cost) if lr.cost is not None else '',
        ]
        for col_i, v in enumerate(vals, start=1):
            ws.cell(row_i, col_i, v).font = norm

    return len(combined)


@login_required
def matched_lmrb_excel(request):
    """
    Export all matched LMRB rows (commercial auto-matched + sponsorship manually
    matched) for the selected scope as a standalone Excel file.
    """
    import openpyxl

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')

    if not (account_id and channel and month):
        return HttpResponse('Missing parameters: account_id, channel, month', status=400)
    if not _account_access(request.user, account_id):
        return HttpResponse('Access denied', status=403)

    account = get_object_or_404(Account, id=account_id)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Matched LMRB'

    _write_matched_lmrb_sheet(ws, account_id, channel, month)

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    fname = f'Matched_LMRB_{account.name}_{channel}_{month}.xlsx'.replace(' ', '_')
    resp = HttpResponse(
        buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


@login_required
@role_required(['super_admin', 'admin'])
def admin_export(request):
    """
    FIX 6 — Admin-only full database export.

    GET:  Show the filter form.
    POST: Generate and download the Excel file.

    Exports to Excel with separate sheets:
      - Matched LMRB (is_matched=True)
      - Unmatched LMRB (is_matched=False)
      - Summary (build_summary_data output)

    Filters: account, channel, brand, date_from, date_to.
    Each sheet has a metadata row at the top with row count.
    """
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from django.db.models import Min, Max

    user       = request.user
    account_qs = _account_qs(user)

    if request.method == 'GET':
        # Collect available channels for the dropdown (filtered by account if selected)
        account_id = request.GET.get('account_id', '')
        channels = []
        if account_id and _account_access(user, account_id):
            channels = sorted(set(
                LMRBRow.objects.filter(account_id=account_id)
                .values_list('channel', flat=True)
            ))
        return render(request, 'admin_export.html', {
            'accounts': account_qs,
            'account_id': account_id,
            'channels': channels,
        })

    # POST — build export
    account_id = request.POST.get('account_id', '').strip()
    channel    = request.POST.get('channel', '').strip()
    brand      = request.POST.get('brand', '').strip()
    date_from  = request.POST.get('date_from', '').strip()
    date_to    = request.POST.get('date_to', '').strip()
    scope      = request.POST.get('scope', 'both')  # 'matched' | 'unmatched' | 'both'

    if not account_id or not _account_access(user, account_id):
        messages.error(request, 'Select a valid account.')
        return redirect('/dashboard/admin-export/')

    account = get_object_or_404(Account, id=account_id)

    # Base queryset
    base_qs = LMRBRow.objects.filter(account_id=account_id)
    if channel:
        base_qs = base_qs.filter(channel__iexact=channel)
    if brand:
        # Filter by advt_theme containing brand (case-insensitive)
        base_qs = base_qs.filter(advt_theme__icontains=brand)
    if date_from:
        try:
            from datetime import date as _date
            from_d = _date.fromisoformat(date_from)
            base_qs = base_qs.filter(date__gte=from_d)
        except ValueError:
            pass
    if date_to:
        try:
            from datetime import date as _date
            to_d = _date.fromisoformat(date_to)
            base_qs = base_qs.filter(date__lte=to_d)
        except ValueError:
            pass

    matched_qs   = base_qs.filter(is_matched=True).order_by('date', 'advt_time')
    unmatched_qs = base_qs.filter(is_matched=False).order_by('date', 'advt_time')

    wb = openpyxl.Workbook()

    HDR_FILL  = PatternFill('solid', fgColor='0F2340')
    META_FILL = PatternFill('solid', fgColor='FEF9C3')
    hdr_font  = Font(bold=True, color='FFFFFF', size=10)
    meta_font = Font(bold=True, size=10)
    norm_font = Font(size=10)
    centre_al = Alignment(horizontal='center', vertical='center')

    LMRB_HEADERS = [
        'Date', 'Channel', 'Advt_Theme', 'Advt_Time', 'Duration', 'Source',
        'Product_Group', 'Advertiser', 'Product', 'Ads', 'Program', 'Prog_Time',
        'Ad_Pos', 'Tot_Ads', 'Brk_No', 'Pos_In_Brk', 'Ads_In_Brk',
        'Lng', 'Cost', 'Day', 'Is_Matched', 'Is_Sponsorship_Matched',
    ]

    def _write_lmrb_sheet(ws_sheet, qs, label):
        rows = list(qs)
        # Row 1: metadata
        ws_sheet.merge_cells(f'A1:V1')
        c = ws_sheet.cell(1, 1, f'{label} | Account: {account.name}'
                                f'{" | Channel: " + channel if channel else ""}'
                                f' | Total rows: {len(rows)}')
        c.font = meta_font; c.fill = META_FILL
        # Row 2: headers
        for col_i, h in enumerate(LMRB_HEADERS, start=1):
            c = ws_sheet.cell(2, col_i, h)
            c.font = hdr_font; c.fill = HDR_FILL; c.alignment = centre_al
        # Data rows
        for row_i, lr in enumerate(rows, start=3):
            ws_sheet.cell(row_i,  1, str(lr.date) if lr.date else '')
            ws_sheet.cell(row_i,  2, lr.channel or '')
            ws_sheet.cell(row_i,  3, lr.advt_theme or '')
            ws_sheet.cell(row_i,  4, lr.advt_time or '')
            ws_sheet.cell(row_i,  5, lr.duration)
            ws_sheet.cell(row_i,  6, lr.source or '')
            ws_sheet.cell(row_i,  7, lr.product_group or '')
            ws_sheet.cell(row_i,  8, lr.advertiser or '')
            ws_sheet.cell(row_i,  9, lr.product or '')
            ws_sheet.cell(row_i, 10, lr.ads or '')
            ws_sheet.cell(row_i, 11, lr.program or '')
            ws_sheet.cell(row_i, 12, lr.prog_time or '')
            ws_sheet.cell(row_i, 13, lr.ad_pos)
            ws_sheet.cell(row_i, 14, lr.tot_ads)
            ws_sheet.cell(row_i, 15, lr.brk_no)
            ws_sheet.cell(row_i, 16, lr.pos_in_brk)
            ws_sheet.cell(row_i, 17, lr.ads_in_brk)
            ws_sheet.cell(row_i, 18, lr.lng or '')
            ws_sheet.cell(row_i, 19, float(lr.cost) if lr.cost is not None else '')
            ws_sheet.cell(row_i, 20, lr.day or '')
            ws_sheet.cell(row_i, 21, 'Yes' if lr.is_matched else 'No')
            ws_sheet.cell(row_i, 22, 'Yes' if lr.is_sponsorship_matched else 'No')
        for row_i in range(3, len(rows) + 3):
            for col_i in range(1, len(LMRB_HEADERS) + 1):
                ws_sheet.cell(row_i, col_i).font = norm_font

    if scope in ('matched', 'both'):
        ws_matched = wb.active
        ws_matched.title = 'Matched LMRB'
        _write_lmrb_sheet(ws_matched, matched_qs, 'Matched LMRB')

    if scope in ('unmatched', 'both'):
        ws_unmatched = wb.create_sheet('Unmatched LMRB')
        _write_lmrb_sheet(ws_unmatched, unmatched_qs, 'Unmatched LMRB')
    elif scope == 'unmatched':
        # Only unmatched requested — rename the default sheet
        ws_un = wb.active
        ws_un.title = 'Unmatched LMRB'
        _write_lmrb_sheet(ws_un, unmatched_qs, 'Unmatched LMRB')

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    fname = f'AdminExport_{account.name}{"_" + channel if channel else ""}.xlsx'.replace(' ', '_')
    resp = HttpResponse(
        buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


# ── System Settings (super_admin only) ────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin'])
def system_settings(request):
    """
    Site-wide configuration page — super_admin only.

    On first visit, any missing settings are auto-created from SETTING_DEFAULTS
    so the page always shows the full list even on a fresh installation.
    """
    # Ensure all default settings exist in DB
    _ensure_defaults()

    if request.method == 'POST':
        updated = 0
        for s in SystemSetting.objects.all():
            new_val = request.POST.get(s.key, '').strip()
            if s.value != new_val:
                s.value = new_val
                s.save()
                updated += 1
        if updated:
            messages.success(request, f'Settings saved — {updated} value(s) updated.')
        else:
            messages.success(request, 'Settings saved (no changes).')
        return redirect('system_settings')

    # Group settings by their category display label
    from collections import OrderedDict
    CATEGORY_ORDER = ['reconciliation', 'tc_parsing', 'lmrb_parsing']
    CATEGORY_LABELS = {
        'reconciliation': 'Reconciliation',
        'tc_parsing':     'TC File Parsing',
        'lmrb_parsing':   'LMRB / MapOnline File Parsing',
    }
    all_settings = list(SystemSetting.objects.all())
    categories = OrderedDict()
    for cat_key in CATEGORY_ORDER:
        cat_settings = [s for s in all_settings if s.category == cat_key]
        if cat_settings:
            categories[CATEGORY_LABELS.get(cat_key, cat_key)] = cat_settings

    return render(request, 'admin_panel/settings.html', {
        'categories': categories,
        'branding_logo_url':   _branding_url('logo'),
        'branding_tartan_url': _branding_url('tartan'),
    })


# ── Branding upload ─────────────────────────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin'])
def branding_upload(request):
    """Upload logo or tartan pattern image for the login page / sidebar."""
    if request.method == 'POST':
        asset_type = request.POST.get('asset_type')   # 'logo' or 'tartan'
        uploaded   = request.FILES.get('file')
        if asset_type not in ('logo', 'tartan') or not uploaded:
            messages.error(request, 'Invalid upload — specify logo or tartan and provide a file.')
            return redirect('system_settings')
        # Determine extension
        orig_name = uploaded.name.lower()
        ext = '.png'
        for e in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'):
            if orig_name.endswith(e):
                ext = e
                break
        dest_dir = os.path.join(django_settings.MEDIA_ROOT, 'branding')
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, f'{asset_type}{ext}')
        # Remove previous files with different extensions
        for e in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'):
            old = os.path.join(dest_dir, f'{asset_type}{e}')
            if old != dest_path and os.path.exists(old):
                os.remove(old)
        with open(dest_path, 'wb') as fh:
            for chunk in uploaded.chunks():
                fh.write(chunk)
        label = 'Logo' if asset_type == 'logo' else 'Tartan pattern'
        messages.success(request, f'{label} uploaded successfully.')
    return redirect('system_settings')


# ── Sponsorship Reconciliation ─────────────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head', 'planner'])
@require_POST
def sponsorship_reconcile(request):
    """
    POST: Run auto Step 1 sponsorship reconciliation for a scope.
    Accepts mode='smart' (default) or mode='reset'.
    """
    from verification.sponsorship_engine import reconcile_sponsorship

    account_id  = request.POST.get('account_id', '').strip()
    channel     = request.POST.get('channel', '').strip()
    month       = request.POST.get('month', '').strip()
    mode        = request.POST.get('mode', 'smart')
    schedule_id = request.POST.get('schedule_id', '').strip()

    if not (account_id and channel and month):
        messages.error(request, 'Incomplete parameters.')
        return redirect('/dashboard/summary/')
    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/summary/')

    sid = int(schedule_id) if schedule_id else None
    result = reconcile_sponsorship(int(account_id), channel, month, mode=mode, schedule_id=sid)
    messages.success(
        request,
        f'Sponsorship reconciliation complete: '
        f'{result["assigned"]} new assignments, '
        f'{result["already_assigned"]} already assigned, '
        f'{result["total_spon_rows"]} total sponsorship rows.'
    )
    redir = f'/dashboard/summary/?account_id={account_id}&channel={channel}&month={month}'
    if schedule_id:
        redir += f'&schedule_id={schedule_id}'
    return redirect(redir)


@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head'])
def sponsorship_candidates(request):
    """
    GET (AJAX): Return unmatched LMRBRow list available for manual sponsorship
    assignment.  Optionally filtered by brand and duration query params.
    """
    from verification.sponsorship_engine import lmrb_candidates

    account_id = request.GET.get('account_id', '').strip()
    channel    = request.GET.get('channel', '').strip()
    month      = request.GET.get('month', '').strip()
    brand      = request.GET.get('brand', '').strip()
    duration   = request.GET.get('duration', '').strip()

    if not (account_id and channel and month):
        return JsonResponse({'error': 'Incomplete parameters.'}, status=400)
    if not _account_access(request.user, account_id):
        return JsonResponse({'error': 'Access denied.'}, status=403)

    rows = lmrb_candidates(int(account_id), channel, month)

    # Optional server-side filter by brand mapping theme
    if brand or duration:
        from core.models import BrandMapping as BM
        themes = set()
        for bm in BM.objects.filter(account_id=account_id):
            if brand and bm.brand.lower().strip() != brand.lower().strip():
                continue
            if duration:
                try:
                    if bm.duration is not None and int(bm.duration) != int(duration):
                        continue
                except (ValueError, TypeError):
                    pass
            if bm.theme:
                themes.add(bm.theme.lower().strip())

        if themes:
            rows = [
                r for r in rows
                if str(r.get('advt_theme', '')).lower().strip() in themes
            ]
        if duration:
            try:
                dur_int = int(duration)
                rows = [r for r in rows if r.get('duration') == dur_int]
            except ValueError:
                pass

    for r in rows:
        if hasattr(r.get('date'), 'isoformat'):
            r['date'] = r['date'].isoformat()

    return JsonResponse({'candidates': rows})


@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head', 'planner'])
@require_POST
def sponsorship_assign(request):
    """
    POST (AJAX): Manually assign LMRB rows to sponsorship schedule rows.
    Body: JSON {"assignments": [[schedule_row_id, lmrb_row_id], ...],
                "account_id": ..., "channel": ..., "month": ...}
    """
    import json as _json
    from verification.sponsorship_engine import manual_assign

    try:
        body = _json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid JSON.'}, status=400)

    account_id = str(body.get('account_id', '')).strip()
    channel    = str(body.get('channel', '')).strip()
    month      = str(body.get('month', '')).strip()

    if not (account_id and channel and month):
        return JsonResponse({'error': 'Incomplete parameters.'}, status=400)
    if not _account_access(request.user, account_id):
        return JsonResponse({'error': 'Access denied.'}, status=403)

    try:
        assignments = [(int(sr), int(lr)) for sr, lr in body.get('assignments', [])]
    except (ValueError, TypeError, KeyError):
        return JsonResponse({'error': 'Invalid assignments payload.'}, status=400)

    result = manual_assign(int(account_id), channel, month, assignments, request.user)
    return JsonResponse(result)


@login_required
@role_required(['super_admin', 'admin', 'team_head', 'planner'])
@require_POST
def sponsorship_reset(request):
    """POST: Delete all SponsorshipLmrbAssignments for a scope and unlock LMRBRows."""
    from verification.sponsorship_engine import reset_sponsorship

    account_id = request.POST.get('account_id', '').strip()
    channel    = request.POST.get('channel', '').strip()
    month      = request.POST.get('month', '').strip()

    if not (account_id and channel and month):
        messages.error(request, 'Incomplete parameters.')
        return redirect('/dashboard/summary/')
    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/summary/')

    result = reset_sponsorship(int(account_id), channel, month)
    messages.success(
        request,
        f'Sponsorship reconciliation reset: '
        f'{result["deleted"]} assignments removed, '
        f'{result["lmrb_unlocked"]} LMRB rows unlocked.'
    )
    return redirect(
        f'/dashboard/summary/?account_id={account_id}&channel={channel}&month={month}'
    )


@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head'])
def sponsorship_unmatched_rows(request):
    """
    GET (AJAX): Return unmatched SPONSORSHIP ScheduleRow ids for a brand/duration/month,
    so the manual picker can pair selected LMRB rows with pending schedule rows.
    """
    account_id = request.GET.get('account_id', '').strip()
    channel    = request.GET.get('channel', '').strip()
    month      = request.GET.get('month', '').strip()
    brand      = request.GET.get('brand', '').strip()
    duration   = request.GET.get('duration', '').strip()

    if not (account_id and channel and month):
        return JsonResponse({'error': 'Incomplete parameters.'}, status=400)
    if not _account_access(request.user, account_id):
        return JsonResponse({'error': 'Access denied.'}, status=403)

    qs = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month,
        ad_type='SPONSORSHIP', brand=brand,
    ).exclude(sponsorship_assignment__isnull=False)

    if duration:
        try:
            qs = qs.filter(duration=int(duration))
        except ValueError:
            pass

    ids = list(qs.order_by('date', 'start_time').values_list('id', flat=True))
    return JsonResponse({'schedule_row_ids': ids})


# ── Manual Reconciliation ──────────────────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head', 'planner'])
def manual_reconciliation(request):
    """
    Three-panel view: unmatched schedule rows (left), unmatched TC spots (middle),
    unmatched LMRB rows (right). Supports Schedule+LMRB, 3-way, and TC+LMRB modes.
    Existing ManualMatch records for the scope are shown below with a De-match button.
    """
    from core.models import ManualMatch

    user       = request.user
    account_qs = _account_qs(user)

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')

    channels           = []
    months             = []
    unmatched_schedule = []
    unmatched_tc       = []
    unmatched_lmrb     = []
    manual_matches     = []
    sch_date_range     = (None, None)

    if account_id:
        if _account_access(user, account_id):
            channels = list(
                ScheduleRow.objects.filter(account_id=account_id)
                .values_list('channel', flat=True).distinct().order_by('channel')
            )

    if account_id and channel:
        if _account_access(user, account_id):
            months = list(
                ScheduleRow.objects.filter(account_id=account_id, channel=channel)
                .values_list('month', flat=True).distinct().order_by('month')
            )

    if account_id and channel and month and _account_access(user, account_id):
        # Sponsorship schedule rows that have already been matched via
        # SponsorshipLmrbAssignment must not appear in the unmatched panel.
        from core.models import SponsorshipLmrbAssignment as _SLA
        matched_spon_sr_ids = set(
            _SLA.objects.filter(
                account_id=account_id,
                schedule_row__channel=channel,
                schedule_row__month=month,
            ).values_list('schedule_row_id', flat=True)
        )

        # Unmatched schedule rows (both COMMERCIAL BENEFITS and SPONSORSHIP)
        unmatched_schedule = list(
            ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type__in=['COMMERCIAL BENEFITS', 'SPONSORSHIP'],
                is_matched=False,
                is_manual_matched=False,
            ).exclude(id__in=matched_spon_sr_ids)
            .order_by('date', 'start_time', 'brand')
        )

        # Determine schedule date range for display purposes
        from django.db.models import Min as _Min, Max as _Max
        dr = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).aggregate(mn=_Min('date'), mx=_Max('date'))
        sch_date_range = (dr['mn'], dr['mx'])

        # Unmatched TC rows — exclude auto-matched and extra rows, and rows
        # that already have a ManualMatch linking them.
        unmatched_tc = list(
            TCRow.objects.filter(
                account_id=account_id,
                channel=channel,
                tc_report__month=month,
                manual_match__isnull=True,
                is_schedule_matched=False,
                is_extra=False,
            ).select_related('tc_report')
            .order_by('tc_theme', 'duration', 'date', 'aired_time')[:500]
        )

        # Unmatched LMRB rows for this channel — NOT date-filtered so out-of-range
        # rows (the whole purpose of manual matching) are visible.
        unmatched_lmrb = list(
            LMRBRow.objects.filter(
                account_id=account_id,
                channel__iexact=channel,
                is_matched=False,
                is_manual_matched=False,
                is_sponsorship_matched=False,
            ).order_by('advt_theme', 'duration', 'date', 'advt_time')[:500]
        )

        manual_matches = list(
            ManualMatch.objects.filter(
                account_id=account_id, channel=channel, month=month,
            ).select_related('schedule_row', 'tc_row', 'lmrb_row', 'matched_by')
        )

    return render(request, 'manual_reconciliation/index.html', {
        'accounts':           account_qs,
        'account_id':         account_id,
        'channels':           channels,
        'channel':            channel,
        'months':             months,
        'month':              month,
        'unmatched_schedule': unmatched_schedule,
        'unmatched_tc':       unmatched_tc,
        'unmatched_lmrb':     unmatched_lmrb,
        'manual_matches':     manual_matches,
        'sch_date_range':     sch_date_range,
    })


@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head', 'planner'])
@require_POST
def manual_match_create(request):
    """
    POST: Create a ManualMatch.  Supports three modes:
      schedule_lmrb — Schedule + LMRB  (original)
      3way          — Schedule + TC + LMRB
      tc_lmrb       — TC + LMRB only (no schedule row)
    """
    from core.models import ManualMatch, MatchResult

    account_id      = request.POST.get('account_id', '').strip()
    channel         = request.POST.get('channel', '').strip()
    month           = request.POST.get('month', '').strip()
    match_mode      = request.POST.get('match_mode', 'schedule_lmrb').strip()
    schedule_row_id = request.POST.get('schedule_row_id', '').strip()
    tc_row_id       = request.POST.get('tc_row_id', '').strip()
    lmrb_row_id     = request.POST.get('lmrb_row_id', '').strip()
    note            = request.POST.get('note', '').strip()

    redirect_url = (
        f'/dashboard/manual/?account_id={account_id}'
        f'&channel={channel}&month={month}'
    )

    if not all([account_id, channel, month, lmrb_row_id]):
        messages.error(request, 'Incomplete parameters.')
        return redirect(redirect_url)
    if match_mode in ('schedule_lmrb', '3way') and not schedule_row_id:
        messages.error(request, 'A schedule row is required for this match mode.')
        return redirect(redirect_url)
    if match_mode in ('3way', 'tc_lmrb') and not tc_row_id:
        messages.error(request, 'A TC row is required for this match mode.')
        return redirect(redirect_url)
    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect(redirect_url)

    # Fetch schedule row if needed
    sr = None
    if schedule_row_id:
        sr = get_object_or_404(
            ScheduleRow, id=schedule_row_id, account_id=account_id,
            channel=channel, month=month,
            is_matched=False, is_manual_matched=False,
        )

    # Fetch TC row if needed
    tr = None
    if tc_row_id:
        tr = get_object_or_404(
            TCRow, id=tc_row_id, account_id=account_id, channel=channel,
            manual_match__isnull=True,
        )

    # Fetch LMRB row
    lr = get_object_or_404(
        LMRBRow, id=lmrb_row_id, account_id=account_id,
        is_matched=False, is_manual_matched=False, is_sponsorship_matched=False,
    )

    # Race-condition guards
    if ManualMatch.objects.filter(lmrb_row=lr).exists():
        messages.error(request, 'LMRB row already manually matched.')
        return redirect(redirect_url)
    if sr and ManualMatch.objects.filter(schedule_row=sr).exists():
        messages.error(request, 'Schedule row already manually matched.')
        return redirect(redirect_url)
    if tr and ManualMatch.objects.filter(tc_row=tr).exists():
        messages.error(request, 'TC row already manually matched.')
        return redirect(redirect_url)

    # Create the manual match
    ManualMatch.objects.create(
        account_id   = account_id,
        channel      = channel,
        month        = month,
        match_mode   = match_mode,
        schedule_row = sr,
        tc_row       = tr,
        lmrb_row     = lr,
        note         = note,
        matched_by   = request.user,
    )

    # Lock rows
    if sr:
        ScheduleRow.objects.filter(id=sr.id).update(is_manual_matched=True)
    LMRBRow.objects.filter(id=lr.id).update(is_manual_matched=True)

    # For 3way and tc_lmrb modes: also lock the TCRow so it appears as matched
    # in TC reconciliation views (is_schedule_matched=True on TCRow).
    if tr and match_mode in ('3way', 'tc_lmrb'):
        update_fields = {'is_schedule_matched': True}
        if sr:
            update_fields['matched_schedule_id'] = sr.id
        TCRow.objects.filter(id=tr.id).update(**update_fields)

    # Remove stale engine MatchResult for this schedule row
    if sr:
        MatchResult.objects.filter(
            schedule_row=sr,
            status__in=['not_aired', 'late_telecast', 'programme_mismatch'],
        ).delete()

    if sr:
        desc = f'{sr.brand} ({sr.duration}s, {sr.date})'
    elif tr:
        desc = f'TC: {tr.tc_theme} ({tr.date})'
    else:
        desc = 'unknown'
    messages.success(
        request,
        f'Manually matched [{match_mode}]: {desc} ← LMRB: {lr.advt_theme} ({lr.date} {lr.advt_time})',
    )
    return redirect(redirect_url)


@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head', 'planner'])
@require_POST
def manual_dematch(request, pk):
    """
    POST: Remove a ManualMatch and unlock both rows.
    """
    from core.models import ManualMatch

    account_id = request.POST.get('account_id', '').strip()
    channel    = request.POST.get('channel', '').strip()
    month      = request.POST.get('month', '').strip()

    redirect_url = (
        f'/dashboard/manual/?account_id={account_id}'
        f'&channel={channel}&month={month}'
    )

    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect(redirect_url)

    mm = get_object_or_404(ManualMatch, pk=pk, account_id=account_id)

    sr_id = mm.schedule_row_id
    lr_id = mm.lmrb_row_id
    if mm.schedule_row_id:
        sr_desc = f'{mm.schedule_row.brand} ({mm.schedule_row.date})'
    elif mm.tc_row_id:
        sr_desc = f'TC: {mm.tc_row.tc_theme} ({mm.tc_row.date})'
    else:
        sr_desc = 'entry'

    tc_row_id_to_unlock = mm.tc_row_id
    tc_mode = mm.match_mode

    mm.delete()

    # Unlock rows
    if sr_id:
        ScheduleRow.objects.filter(id=sr_id).update(is_manual_matched=False)
    LMRBRow.objects.filter(id=lr_id).update(is_manual_matched=False)

    # Unlock TCRow if it was locked by this manual match
    if tc_row_id_to_unlock and tc_mode in ('3way', 'tc_lmrb'):
        TCRow.objects.filter(id=tc_row_id_to_unlock).update(
            is_schedule_matched=False, matched_schedule=None,
        )

    messages.success(request, f'De-matched: {sr_desc}. All rows are now available again.')
    return redirect(redirect_url)


# ── Commercial Tags Pool ───────────────────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head'])
def commercial_candidates(request):
    """
    GET (AJAX): Return unmatched LMRBRow list available for manual commercial
    Tags assignment.  Filtered by brand theme mapping and optionally by duration.
    Only returns rows that are not commercially matched, not sponsorship matched,
    and not manually matched (i.e., fully available).
    """
    from django.db.models import Q as _Q, Min as _Min, Max as _Max

    account_id = request.GET.get('account_id', '').strip()
    channel    = request.GET.get('channel', '').strip()
    month      = request.GET.get('month', '').strip()
    brand      = request.GET.get('brand', '').strip()
    duration   = request.GET.get('duration', '').strip()

    if not (account_id and channel and month):
        return JsonResponse({'error': 'Incomplete parameters.'}, status=400)
    if not _account_access(request.user, account_id):
        return JsonResponse({'error': 'Access denied.'}, status=403)

    # Get scope date range from schedule
    agg = Schedule.objects.filter(account_id=account_id, channel=channel, month=month
        ).aggregate(d_min=_Min('start_date'), d_max=_Max('end_date'))
    d_min, d_max = agg['d_min'], agg['d_max']
    if not d_min or not d_max:
        ragg = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month,
        ).aggregate(d_min=_Min('date'), d_max=_Max('date'))
        d_min = d_min or ragg['d_min']
        d_max = d_max or ragg['d_max']

    # Base queryset: fully unmatched LMRB rows in scope
    qs = LMRBRow.objects.filter(
        account_id=account_id, channel__iexact=channel,
        is_matched=False, is_sponsorship_matched=False, is_manual_matched=False,
    )
    if d_min:
        qs = qs.filter(date__gte=d_min)
    if d_max:
        qs = qs.filter(date__lte=d_max)

    # Filter by theme matching the brand
    if brand:
        themes = set()
        for bm in BrandMapping.objects.filter(account_id=account_id):
            if bm.brand.lower().strip() != brand.lower().strip():
                continue
            if duration:
                try:
                    if bm.duration is not None and int(bm.duration) != int(duration):
                        continue
                except (ValueError, TypeError):
                    pass
            if bm.theme:
                themes.add(bm.theme.lower().strip())
        if themes:
            theme_q = _Q()
            for t in themes:
                theme_q |= _Q(advt_theme__iexact=t)
            qs = qs.filter(theme_q)

    if duration:
        try:
            qs = qs.filter(duration=int(duration))
        except ValueError:
            pass

    rows = list(qs.order_by('advt_theme', 'date', 'advt_time').values(
        'id', 'date', 'advt_time', 'advt_theme', 'duration', 'program', 'source',
    ))
    for r in rows:
        if hasattr(r.get('date'), 'isoformat'):
            r['date'] = r['date'].isoformat()

    return JsonResponse({'candidates': rows})


@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head'])
def commercial_unmatched_rows(request):
    """
    GET (AJAX): Return unmatched COMMERCIAL BENEFITS ScheduleRow IDs for a
    brand/duration/month so the picker can pair LMRB rows with schedule slots.
    """
    account_id = request.GET.get('account_id', '').strip()
    channel    = request.GET.get('channel', '').strip()
    month      = request.GET.get('month', '').strip()
    brand      = request.GET.get('brand', '').strip()
    duration   = request.GET.get('duration', '').strip()

    if not (account_id and channel and month):
        return JsonResponse({'error': 'Incomplete parameters.'}, status=400)
    if not _account_access(request.user, account_id):
        return JsonResponse({'error': 'Access denied.'}, status=403)

    qs = ScheduleRow.objects.filter(
        account_id=account_id, channel=channel, month=month,
        ad_type='COMMERCIAL BENEFITS', brand=brand,
        is_matched=False, is_manual_matched=False,
    )
    if duration:
        try:
            qs = qs.filter(duration=int(duration))
        except ValueError:
            pass

    ids = list(qs.order_by('date', 'start_time').values_list('id', flat=True))
    return JsonResponse({'schedule_row_ids': ids})


@login_required
@role_required(['super_admin', 'admin', 'operations', 'team_head', 'planner'])
@require_POST
def commercial_assign(request):
    """
    POST (AJAX): Manually assign LMRB rows to commercial BENEFITS schedule rows
    as 'Tags'.  Creates ManualMatch records (schedule_lmrb mode) which are then
    counted in build_summary_data's manual_aired column.

    Body: JSON {
      "account_id": ..., "channel": ..., "month": ...,
      "assignments": [[schedule_row_id, lmrb_row_id], ...]
    }
    """
    import json as _json
    from core.models import ManualMatch as _MM

    try:
        body = _json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'error': 'Invalid JSON.'}, status=400)

    account_id = str(body.get('account_id', '')).strip()
    channel    = str(body.get('channel', '')).strip()
    month      = str(body.get('month', '')).strip()

    if not (account_id and channel and month):
        return JsonResponse({'error': 'Incomplete parameters.'}, status=400)
    if not _account_access(request.user, account_id):
        return JsonResponse({'error': 'Access denied.'}, status=403)

    try:
        assignments = [(int(sr), int(lr)) for sr, lr in body.get('assignments', [])]
    except (ValueError, TypeError, KeyError):
        return JsonResponse({'error': 'Invalid assignments payload.'}, status=400)

    created = 0
    skipped = 0

    for sr_id, lr_id in assignments:
        try:
            sr = ScheduleRow.objects.get(
                id=sr_id, account_id=account_id, channel=channel,
                month=month, ad_type='COMMERCIAL BENEFITS',
                is_matched=False, is_manual_matched=False,
            )
        except ScheduleRow.DoesNotExist:
            skipped += 1
            continue

        try:
            lr = LMRBRow.objects.get(
                id=lr_id, account_id=account_id,
                is_matched=False, is_sponsorship_matched=False,
                is_manual_matched=False,
            )
        except LMRBRow.DoesNotExist:
            skipped += 1
            continue

        # Race-condition guard
        if _MM.objects.filter(lmrb_row=lr).exists() or _MM.objects.filter(schedule_row=sr).exists():
            skipped += 1
            continue

        _MM.objects.create(
            account_id=account_id, channel=channel, month=month,
            match_mode='schedule_lmrb',
            schedule_row=sr, lmrb_row=lr,
            note='Commercial Tags – manually assigned via summary picker',
            matched_by=request.user,
        )
        ScheduleRow.objects.filter(id=sr_id).update(is_manual_matched=True)
        LMRBRow.objects.filter(id=lr_id).update(is_manual_matched=True)
        created += 1

    return JsonResponse({'created': created, 'skipped': skipped})


# ── Database Admin Tools ───────────────────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin'])
def db_tools(request):
    """
    Database management page — admin/super_admin only.
    Provides data reset, selective delete, and database backup tools.
    """
    from django.db import connection
    from django.db.models import Count as _Count

    def _counts():
        return {
            'schedules':    Schedule.objects.count(),
            'schedule_rows': ScheduleRow.objects.count(),
            'lmrb_rows':    LMRBRow.objects.count(),
            'monitoring':   MonitoringData.objects.count(),
            'match_results': MatchResult.objects.count(),
            'tc_rows':      TCRow.objects.count(),
            'tc_reports':   TransmissionReport.objects.count(),
        }

    msg = None
    msg_type = 'success'

    if request.method == 'POST':
        action = request.POST.get('action')
        confirm = request.POST.get('confirm', '')

        if action == 'reset_match_results':
            n = MatchResult.objects.exclude(status='manual_match').delete()[0]
            # Also reset is_matched flags on ScheduleRow and LMRBRow
            ScheduleRow.objects.filter(is_matched=True).update(
                is_matched=False, matched_lmrb=None, matched_at=None
            )
            LMRBRow.objects.filter(is_matched=True).update(
                is_matched=False, matched_schedule=None, matched_at=None
            )
            msg = f'Reset complete: {n} MatchResult records deleted and row locks cleared. Re-run verification to rebuild results.'

        elif action == 'delete_duplicate_schedules':
            # Keep only the highest-version Schedule per (account, channel, month, schedule_number)
            from django.db.models import Max as _Max
            keeper_ids = set()
            for combo in Schedule.objects.values('account_id', 'channel', 'month', 'schedule_number').distinct():
                best = Schedule.objects.filter(**combo).order_by('-version').values_list('id', flat=True).first()
                if best:
                    keeper_ids.add(best)
            dupes = Schedule.objects.exclude(id__in=keeper_ids)
            n_schedules = dupes.count()
            if n_schedules == 0:
                msg = 'No duplicate schedules found — database is already clean.'
                msg_type = 'info'
            else:
                # Delete ScheduleRows belonging to duplicate schedules, then the schedules
                n_rows = ScheduleRow.objects.filter(schedule__in=dupes).count()
                MatchResult.objects.filter(schedule_row__schedule__in=dupes).delete()
                ScheduleRow.objects.filter(schedule__in=dupes).delete()
                dupes.delete()
                msg = (f'Removed {n_schedules} duplicate Schedule record(s) and {n_rows} ScheduleRow(s). '
                       f'Only the latest version of each schedule number is kept.')

        elif action == 'delete_schedules' and confirm == 'DELETE':
            MatchResult.objects.all().delete()
            ScheduleRow.objects.all().delete()
            Schedule.objects.all().delete()
            msg = 'All schedule data deleted.'

        elif action == 'delete_lmrb' and confirm == 'DELETE':
            LMRBRow.objects.all().delete()
            MonitoringData.objects.all().delete()
            msg = 'All LMRB / monitoring data deleted.'

        elif action == 'delete_tc' and confirm == 'DELETE':
            TCRow.objects.all().delete()
            TransmissionReport.objects.all().delete()
            msg = 'All TC data deleted.'

        elif action == 'delete_all' and confirm == 'DELETE_ALL':
            MatchResult.objects.all().delete()
            ScheduleRow.objects.all().delete()
            Schedule.objects.all().delete()
            LMRBRow.objects.all().delete()
            MonitoringData.objects.all().delete()
            TCRow.objects.all().delete()
            TransmissionReport.objects.all().delete()
            # Reset row locks
            ScheduleRow.objects.update(is_matched=False, matched_lmrb=None, matched_at=None)
            LMRBRow.objects.update(is_matched=False, matched_schedule=None, matched_at=None)
            msg = 'ALL data deleted. The system is now empty.'

        elif action in ('delete_schedules', 'delete_lmrb', 'delete_tc', 'delete_all'):
            msg = 'Delete cancelled — confirmation text did not match.'
            msg_type = 'error'

        elif action == 'backup_db':
            # Serve the SQLite file directly if running SQLite
            db_path = django_settings.DATABASES['default'].get('NAME', '')
            if db_path and db_path.endswith('.sqlite3') and os.path.exists(db_path):
                import mimetypes
                return FileResponse(
                    open(db_path, 'rb'),
                    as_attachment=True,
                    filename='ad_monitor_backup.sqlite3',
                    content_type='application/octet-stream',
                )
            else:
                msg = ('This deployment uses PostgreSQL. Use Railway\'s built-in backup feature '
                       'or run `pg_dump` from the CLI to export your database.')
                msg_type = 'info'

    return render(request, 'admin_panel/db_tools.html', {
        'counts': _counts(),
        'msg': msg,
        'msg_type': msg_type,
    })
