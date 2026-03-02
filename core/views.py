import io
import json
import os
import uuid
import pandas as pd
from datetime import date as date_cls, datetime

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
    LMRBRow, MonitoringData, Schedule, ScheduleRow,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_admin(user):
    return user.role in ('super_admin', 'admin')


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

            # ── Parse Schedule rows into DB ────────────────────────────────────
            _parse_schedule_rows(df, schedule, account, channel_obj.name, month)

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

    return render(request, 'schedules/upload.html', {'form': form})


def _parse_schedule_rows(df, schedule, account, channel, month):
    """Parse a schedule DataFrame and bulk-create ScheduleRow records."""
    VALID_TYPES = {'COMMERCIAL BENEFITS', 'SPONSORSHIP'}
    rows = []
    for _, r in df.iterrows():
        ad_type = _safe_str(r.get('Advertisement_Type', '')).upper()
        if ad_type not in VALID_TYPES:
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
            try:
                df = pd.read_excel(excel_file)
                df.columns = df.columns.str.strip()
            except Exception as e:
                messages.error(request, f'Cannot read Excel file: {e}')
                return render(request, 'monitoring/upload.html', {'form': form})

            channel_metas = _detect_monitoring_meta(df, data_type)
            if not channel_metas:
                messages.error(request, 'No channels detected in the file.')
                return render(request, 'monitoring/upload.html', {'form': form})

            for meta in channel_metas:
                ch = meta['channel']
                if ch and ch != 'Unknown':
                    Channel.objects.get_or_create(name=ch)

            group_id   = str(uuid.uuid4())
            saved_path = None
            excel_file.seek(0)

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
                else:
                    mon.file = saved_path
                mon.save()

            # ── Parse LMRB rows into master DB table ───────────────────────────
            _parse_lmrb_rows(df, data_type, account)

            ch_names = ', '.join(m['channel'] for m in channel_metas)
            messages.success(request,
                f'{"MapOnline" if data_type == "maponline" else "MediaWatch (LMRB)"} — '
                f'{account} — {len(channel_metas)} channel(s): {ch_names}. Uploaded successfully.')

            # Auto-run verification
            try:
                from verification.engine import auto_run_all_for_account
                auto_run_all_for_account(account.id)
            except Exception:
                pass

            return redirect('/dashboard/monitoring/')

    return render(request, 'monitoring/upload.html', {'form': form})


def _parse_lmrb_rows(df, data_type, account):
    """
    Parse monitoring DataFrame and upsert into LMRBRow master table.

    Dedup key = sha256(account|channel|date|advt_time|advt_theme|duration)[:32].
    If an existing row matches the key:
      - Reset its linked ScheduleRow (unlock it)
      - Delete the old LMRBRow
    Then insert the new row with is_matched=False.
    """
    df = df.copy()

    # ── Normalise to standard column names ─────────────────────────────────────
    if data_type == 'maponline':
        rename = {}
        if 'Theme'    in df.columns: rename['Theme']    = 'Advt_Theme'
        if 'Prg Date' in df.columns: rename['Prg Date'] = 'Date'
        if 'Ad Dur'   in df.columns: rename['Ad Dur']   = 'Dur'
        if 'Ad Start' in df.columns: rename['Ad Start'] = 'Advt_time'
        df.rename(columns=rename, inplace=True)
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')
    else:  # mediawatch
        if {'Dd', 'Mn', 'Yr'}.issubset(df.columns) and 'Date' not in df.columns:
            df['Date'] = pd.to_datetime(
                df['Yr'].astype(str) + '-' +
                df['Mn'].astype(str).str.zfill(2) + '-' +
                df['Dd'].astype(str).str.zfill(2),
                errors='coerce',
            )
        elif 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'], errors='coerce')

    if 'Dur' in df.columns:
        df['Dur'] = pd.to_numeric(df['Dur'], errors='coerce')

    ch_col = 'Channel' if 'Channel' in df.columns else None

    # ── Build rows to upsert ──────────────────────────────────────────────────
    rows_by_key = {}   # dedup_key → LMRBRow instance (to create)

    for _, r in df.iterrows():
        channel   = _safe_str(r.get(ch_col, 'Unknown')) if ch_col else 'Unknown'
        theme     = _safe_str(r.get('Advt_Theme', ''))
        advt_time = _safe_str(r.get('Advt_time', ''))
        dur       = _safe_int(r.get('Dur'))
        date_val  = _safe_date(r.get('Date'))

        if not (theme and advt_time and date_val and channel):
            continue  # skip rows with missing key fields

        dedup_key = LMRBRow.make_dedup_key(account.id, channel, date_val, advt_time, theme, dur)

        # Latest row wins if multiple rows in the same upload share the same key
        rows_by_key[dedup_key] = LMRBRow(
            account    = account,
            channel    = channel,
            date       = date_val,
            advt_theme = theme,
            advt_time  = advt_time,
            duration   = dur,
            source     = data_type,
            dedup_key  = dedup_key,
        )

    if not rows_by_key:
        return

    # ── Handle existing duplicates (unlock linked ScheduleRows first) ──────────
    existing = LMRBRow.objects.filter(dedup_key__in=rows_by_key.keys())
    sch_row_ids_to_unlock = []
    for old in existing:
        if old.is_matched and old.matched_schedule_id:
            sch_row_ids_to_unlock.append(old.matched_schedule_id)

    if sch_row_ids_to_unlock:
        from django.utils import timezone
        ScheduleRow.objects.filter(id__in=sch_row_ids_to_unlock).update(
            is_matched=False,
            matched_lmrb=None,
            matched_at=None,
        )

    # Delete old rows (new ones will be inserted below)
    existing.delete()

    # ── Bulk-create new rows ──────────────────────────────────────────────────
    LMRBRow.objects.bulk_create(list(rows_by_key.values()), batch_size=500)


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

    if not _is_admin(user) and mon.account and mon.account not in _account_qs(user):
        messages.error(request, 'You do not have access to this data.')
        return redirect('/dashboard/monitoring/')

    if _is_admin(user) or (mon.uploaded_by == user and mon.uploaded_at.date() == today):
        siblings = MonitoringData.objects.filter(file_group_id=mon.file_group_id).exclude(pk=mon.pk)
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


# ── LMRB controlled deletion (Item 7) ────────────────────────────────────────

@login_required
@require_POST
def lmrb_delete_range(request):
    """
    Delete LMRBRow records for a specific account + channel + date range.
    Any linked ScheduleRows are automatically unlocked (is_matched → False).
    """
    account_id = request.POST.get('account_id', '').strip()
    channel    = request.POST.get('channel', '').strip()
    date_from  = request.POST.get('date_from', '').strip()
    date_to    = request.POST.get('date_to', '').strip()

    if not account_id or not channel:
        messages.error(request, 'Account and Channel are required.')
        return redirect('/dashboard/monitoring/')

    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/monitoring/')

    qs = LMRBRow.objects.filter(account_id=account_id, channel=channel)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    # Unlock any ScheduleRows that were linked to these LMRB rows
    matched_sch_ids = list(
        qs.filter(is_matched=True).values_list('matched_schedule_id', flat=True)
    )
    if matched_sch_ids:
        ScheduleRow.objects.filter(id__in=matched_sch_ids).update(
            is_matched=False, matched_lmrb=None, matched_at=None,
        )

    count = qs.count()
    qs.delete()
    date_desc = f' ({date_from} → {date_to})' if (date_from or date_to) else ''
    messages.success(request,
        f'Deleted {count:,} LMRB records for {channel}{date_desc}. '
        f'Linked schedule rows have been unlocked for re-matching.')
    return redirect('/dashboard/monitoring/')


# ── Monitoring Dashboard (Items 3 + 4) ───────────────────────────────────────

@login_required
def monitoring_dashboard(request):
    """
    Analytics dashboard: per-scope summary, 7 report tabs, export + PDF links.
    Accessible by operations, team_head, planner, admin, super_admin.
    """
    from core.models import MatchResult, ScheduleRow

    user       = request.user
    account_qs = _account_qs(user)

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')

    selected_account = None
    channels = []
    months   = []
    stats    = {}
    tab_data = {}
    ch_summary   = []
    brand_summary = []
    sponsorship_rows = []

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
        qs    = MatchResult.objects.filter(account_id=account_id, channel=channel, month=month)
        total = qs.count()

        n_matched    = qs.filter(status='matched').count()
        n_prog_mis   = qs.filter(status='programme_mismatch').count()
        n_late       = qs.filter(status='late_telecast').count()
        n_not_aired  = qs.filter(status__in=['not_aired', 'no_mapping']).count()
        n_no_map     = qs.filter(status='no_mapping').count()
        n_aired      = n_matched + n_prog_mis + n_late
        compliance   = round(n_matched / total * 100, 1) if total else 0

        n_sponsorship = ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month, ad_type='SPONSORSHIP',
        ).count()

        stats = {
            'total':      total,
            'matched':    n_matched,
            'prog_mis':   n_prog_mis,
            'late':       n_late,
            'not_aired':  n_not_aired,
            'no_map':     n_no_map,
            'aired':      n_aired,
            'compliance': compliance,
            'sponsorship': n_sponsorship,
        }

        tab_data = {
            'full':     list(qs.order_by('scheduled_date', 'brand')),
            'matched':  list(qs.filter(status='matched').order_by('scheduled_date', 'brand')),
            'not_aired': list(qs.filter(status__in=['not_aired', 'no_mapping']).order_by('scheduled_date', 'brand')),
            'prog_mis': list(qs.filter(status='programme_mismatch').order_by('scheduled_date', 'brand')),
            'late':     list(qs.filter(status='late_telecast').order_by('scheduled_date', 'brand')),
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

        # Sponsorship rows for the separate sponsorship tab
        sponsorship_rows = list(ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month, ad_type='SPONSORSHIP',
        ).order_by('date', 'start_time'))

    return render(request, 'monitoring/dashboard.html', {
        'accounts':         account_qs,
        'selected_account': selected_account,
        'account_id':       account_id,
        'channels':         channels,
        'months':           months,
        'channel':          channel,
        'month':            month,
        'stats':            stats,
        'tab_data':         tab_data,
        'ch_summary':       ch_summary,
        'brand_summary':    brand_summary,
        'sponsorship_rows': sponsorship_rows,
    })


# ── PDF Missed-Ad Report (Item 5) ─────────────────────────────────────────────

@login_required
def monitoring_pdf(request):
    """
    Generate a professional ReportLab PDF of Not-Aired / Programme-Mismatch
    rows for a given scope (account + channel + month).
    """
    from core.models import MatchResult

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

    try:
        pdf_bytes = _build_missed_ad_pdf(account.name, channel, month, list(missed_qs))
    except Exception as e:
        return HttpResponse(f'PDF generation failed: {e}', status=500)

    fname = f'missed_ads_{channel}_{month}.pdf'.replace(' ', '_')
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    return response


def _build_missed_ad_pdf(account_name, channel, month, rows):
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
    story.append(Paragraph(f'Account: {account_name}  |  Month: {month}', h_sub))
    story.append(Paragraph(
        f'Generated: {datetime.now().strftime("%d %B %Y %H:%M")}  |  '
        f'Total missed rows: {len(rows)}',
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
