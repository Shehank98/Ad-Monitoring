import json
import uuid
import pandas as pd
from datetime import date as date_cls

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Min, Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.decorators import role_required
from accounts.models import User
from accounts.views import create_user, edit_user, user_list

from .forms import AccountForm, ChannelForm, MonitoringUploadForm, ScheduleUploadForm
from .models import Account, BrandMapping, Channel, MonitoringData, Schedule


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_admin(user):
    return user.role in ('super_admin', 'admin')


def _account_qs(user):
    """Return the Account queryset this user may see/act on."""
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
    Auto-detect channels, and per-channel date ranges from a monitoring DataFrame.

    Returns:
        list of dicts: [{channel, start_date, end_date, row_count}, ...]
    """
    df = df.copy()
    df.columns = df.columns.str.strip()

    # Build date column
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

    # Detect unique channels
    ch_col = 'Channel' if 'Channel' in df.columns else None
    if ch_col:
        channels = [str(c).strip() for c in df[ch_col].dropna().unique() if str(c).strip()]
    else:
        channels = ['Unknown']

    result = []
    for ch in channels:
        if ch_col:
            ch_df = df[df[ch_col].astype(str).str.strip() == ch]
        else:
            ch_df = df
        dates = ch_df['_date'].dropna()
        result.append({
            'channel':    ch,
            'start_date': dates.min().date() if not dates.empty else None,
            'end_date':   dates.max().date() if not dates.empty else None,
            'row_count':  len(ch_df),
        })
    return result


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

    elif role == 'operations':
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
    qs = Schedule.objects.select_related('account', 'uploaded_by')
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
    return render(request, 'schedules/list.html', {
        'schedules': qs,
        'accounts':  accounts,
        'filters':   {'account': account_id, 'channel': channel, 'month': month},
        'today':     today,
    })


@login_required
@role_required(['planner', 'super_admin', 'admin'])
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
                df = pd.read_excel(excel_file)
            except Exception as e:
                messages.error(request, f'Cannot read Excel file: {e}')
                return render(request, 'schedules/upload.html', {'form': form})

            row_count  = len(df)
            account    = form.cleaned_data['account']
            channel_obj = form.cleaned_data['channel']

            # Auto-detect month and date range
            month, start_date, end_date = _detect_schedule_meta(df)
            if not month:
                month = 'Unknown'

            # Auto-increment version per (account, channel)
            last_ver = (
                Schedule.objects
                .filter(account=account, channel=channel_obj.name)
                .aggregate(Max('version'))['version__max'] or 0
            )
            version = last_ver + 1

            excel_file.seek(0)
            schedule = Schedule(
                account           = account,
                channel           = channel_obj.name,
                month             = month,
                schedule_number   = form.cleaned_data['schedule_number'],
                original_filename = excel_file.name,
                row_count         = row_count,
                start_date        = start_date,
                end_date          = end_date,
                version           = version,
                uploaded_by       = user,
            )
            schedule.file.save(excel_file.name, excel_file)
            schedule.save()
            messages.success(request,
                f'Schedule #{schedule.schedule_number} v{version} for {account} '
                f'({month}, {start_date} → {end_date}) uploaded — {row_count:,} rows.')
            return redirect('/dashboard/schedules/')

    return render(request, 'schedules/upload.html', {'form': form})


@login_required
@require_POST
def schedule_detect(request):
    """AJAX: parse an uploaded schedule file and return detected metadata."""
    excel_file = request.FILES.get('file')
    if not excel_file:
        return JsonResponse({'ok': False, 'error': 'No file provided'})
    try:
        df = pd.read_excel(excel_file)
        month, start_date, end_date = _detect_schedule_meta(df)
        return JsonResponse({
            'ok': True,
            'month':      month,
            'start_date': str(start_date) if start_date else '',
            'end_date':   str(end_date)   if end_date   else '',
            'row_count':  len(df),
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
def schedule_delete(request, pk):
    schedule = get_object_or_404(Schedule, pk=pk)
    user     = request.user
    today    = date_cls.today()

    if _is_admin(user) or (schedule.uploaded_by == user and schedule.uploaded_at.date() == today):
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

    cov_base = MonitoringData.objects.select_related('account')
    if not _is_admin(user):
        cov_base = cov_base.filter(account__in=user.accounts.all())
    coverage = (
        cov_base
        .values('account__name', 'channel', 'data_type')
        .annotate(from_date=Min('start_date'), to_date=Max('end_date'), uploads=Count('id'))
        .order_by('data_type', 'account__name', 'channel')
    )

    today    = date_cls.today()
    accounts = _account_qs(user)
    return render(request, 'monitoring/list.html', {
        'data_list': qs,
        'coverage':  coverage,
        'filters':   {'type': dtype, 'channel': channel, 'account': account_id},
        'accounts':  accounts,
        'today':     today,
    })


@login_required
@role_required(['operations', 'super_admin', 'admin'])
def monitoring_upload(request):
    """
    Upload a MapOnline or LMRB file.

    Channels and date ranges are AUTO-DETECTED from the file — users no longer
    need to specify them. One MonitoringData record is created per detected
    channel, all sharing the same physical file (tracked via file_group_id).
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
            try:
                df = pd.read_excel(excel_file)
                df.columns = df.columns.str.strip()
            except Exception as e:
                messages.error(request, f'Cannot read Excel file: {e}')
                return render(request, 'monitoring/upload.html', {'form': form})

            # Detect channels and per-channel date ranges
            channel_metas = _detect_monitoring_meta(df, data_type)
            if not channel_metas:
                messages.error(request, 'No channels detected in the file.')
                return render(request, 'monitoring/upload.html', {'form': form})

            # Ensure Channel records exist for all detected channels
            for meta in channel_metas:
                ch = meta['channel']
                if ch and ch != 'Unknown':
                    Channel.objects.get_or_create(name=ch)

            group_id = str(uuid.uuid4())
            excel_file.seek(0)
            saved_path = None

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
                    # Save the file once for the first channel record
                    excel_file.seek(0)
                    mon.file.save(excel_file.name, excel_file, save=False)
                    saved_path = mon.file.name
                else:
                    # Reuse the already-saved file path for subsequent channels
                    mon.file = saved_path
                mon.save()

            ch_names = ', '.join(m['channel'] for m in channel_metas)
            messages.success(request,
                f'{MonitoringData.DATA_TYPES[0][1] if data_type == "maponline" else "MediaWatch (LMRB)"} — '
                f'{account} — {len(channel_metas)} channel(s) detected: {ch_names}. '
                f'Uploaded successfully.')
            return redirect('/dashboard/monitoring/')

    return render(request, 'monitoring/upload.html', {'form': form})


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
        return JsonResponse({
            'ok':      True,
            'channels': metas,
            'total_rows': len(df),
        })
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


@login_required
def monitoring_delete(request, pk):
    mon   = get_object_or_404(MonitoringData, pk=pk)
    user  = request.user
    today = date_cls.today()

    if not _is_admin(user) and mon.account and mon.account not in user.accounts.all():
        messages.error(request, 'You do not have access to this data.')
        return redirect('/dashboard/monitoring/')

    if _is_admin(user) or (mon.uploaded_by == user and mon.uploaded_at.date() == today):
        # Only delete the physical file if no other record in the same group uses it
        file_path = mon.file.name
        siblings  = MonitoringData.objects.filter(file_group_id=mon.file_group_id).exclude(pk=mon.pk)
        if not siblings.exists():
            mon.file.delete(save=False)
        mon.delete()
        messages.success(request, 'Dataset deleted.')
    else:
        messages.error(request, 'You can only delete datasets you uploaded today.')
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
            dur_raw  = request.POST.get('duration', '').strip()
            duration = int(dur_raw) if dur_raw.isdigit() else None

            if not (acc_id and brand and theme):
                messages.error(request, 'Account, Brand, and Theme are all required.')
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
                            account=account, brand=brand, theme=theme, duration=duration)
                        dur_str = f' ({duration}s)' if duration else ''
                        messages.success(request, f'Mapping added: {brand} → {theme}{dur_str}')
            return redirect(f'/dashboard/brand-mappings/?account={acc_id}')

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
    if account_id:
        mappings = mappings.filter(account_id=account_id)

    return render(request, 'admin_panel/brand_mappings.html', {
        'mappings': mappings,
        'accounts': account_qs,
        'filters':  {'account': account_id},
    })
