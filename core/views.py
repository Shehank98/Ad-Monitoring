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
    LMRBRow, MatchResult, MonitoringData, Schedule, ScheduleRow,
    SummaryReportMeta, TCRow, TransmissionReport,
)


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

    today    = date_cls.today()
    accounts = _account_qs(user)
    return render(request, 'monitoring/list.html', {
        'data_list':   qs,
        'data_groups': data_groups,
        'coverage':    coverage,
        'filters':     {'type': dtype, 'channel': channel, 'account': account_id},
        'accounts':    accounts,
        'today':       today,
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

            # ── Parse LMRB rows into master DB table ───────────────────────────
            print(f"[monitoring_upload] parsing LMRB rows …")
            _parse_lmrb_rows(df, data_type, account)

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
    df.columns = df.columns.str.strip()  # ensure columns are stripped

    # ── Build a case-insensitive channel name lookup from the Channel model ─────
    # When the LMRB file has "SIRASA TV" but the schedule uses "Sirasa TV", the
    # engine query (channel__iexact) will still work, but storing the canonical
    # name avoids creating duplicate Channel records and keeps the DB consistent.
    _ch_canonical = {c.lower(): c for c in Channel.objects.values_list('name', flat=True)}

    def _canon_channel(raw: str) -> str:
        """
        Return the canonical Channel name for an LMRB channel string.

        LMRB (MapOnline / MediaWatch) files prefix channel names with the medium,
        e.g. "Tv - Sirasa TV" or "Radio - Sirasa FM".  Strip that prefix before
        doing the canonical lookup so the stored LMRBRow.channel matches exactly
        the channel name used in Schedule / TC records.

        Lookup order:
          1. Direct case-insensitive match (handles plain "SIRASA TV" → "Sirasa TV")
          2. Strip "Tv - " / "Radio - " prefix then case-insensitive match
          3. Return the stripped form even when not in Channel model
             (keeps DB clean; avoids storing verbose prefix format)
        """
        raw = str(raw).strip()
        # 1. Direct lookup
        canonical = _ch_canonical.get(raw.lower())
        if canonical:
            return canonical
        # 2. Strip LMRB medium prefix and look up again
        for prefix in ('tv - ', 'radio - '):
            if raw.lower().startswith(prefix):
                stripped = raw[len(prefix):]          # preserve original casing of rest
                canonical = _ch_canonical.get(stripped.lower())
                if canonical:
                    return canonical
                # Not in Channel model yet — store stripped form so it can still
                # match a future schedule that uses the same bare channel name.
                return stripped
        return raw

    # ── Normalise to standard column names ─────────────────────────────────────
    if data_type == 'maponline':
        rename = {}
        th_col = _find_col(df, 'Advt_Theme', 'Theme')
        if th_col and th_col != 'Advt_Theme': rename[th_col] = 'Advt_Theme'
        dt_col = _find_col(df, 'Date', 'Prg Date')
        if dt_col and dt_col != 'Date': rename[dt_col] = 'Date'
        du_col = _find_col(df, 'Dur', 'Ad Dur')
        if du_col and du_col != 'Dur': rename[du_col] = 'Dur'
        tm_col = _find_col(df, 'Advt_time', 'Ad Start', 'Advt_Time')
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

    # ── Build rows to upsert ──────────────────────────────────────────────────
    rows_by_key = {}   # dedup_key → LMRBRow instance (to create)

    for _, r in df.iterrows():
        channel   = _canon_channel(_safe_str(r.get(ch_col, 'Unknown')) if ch_col else 'Unknown')
        theme     = _safe_str(r.get('Advt_Theme', ''))
        advt_time = _safe_str(r.get('Advt_time', ''))
        dur       = _safe_int(r.get('Dur'))
        date_val  = _safe_date(r.get('Date'))

        if not (theme and advt_time and date_val and channel):
            continue  # skip rows with missing key fields

        dedup_key = LMRBRow.make_dedup_key(account.id, channel, date_val, advt_time, theme, dur)

        # Map programme from either mediawatch (Program) or maponline (Prg Name)
        program_val = _safe_str(r.get('Program', r.get('Prg Name', '')))

        # Latest row wins if multiple rows in the same upload share the same key
        rows_by_key[dedup_key] = LMRBRow(
            account       = account,
            channel       = channel,
            date          = date_val,
            advt_theme    = theme,
            advt_time     = advt_time,
            duration      = dur,
            source        = data_type,
            dedup_key     = dedup_key,
            # Extended columns
            product_group = _safe_str(r.get('Product_Group', '')),
            advertiser    = _safe_str(r.get('Advertiser', '')),
            product       = _safe_str(r.get('Product', '')),
            ads           = _safe_str(r.get('Ads', '')),
            program       = program_val,
            prog_time     = _safe_str(r.get('Prog_time', '')),
            ad_pos        = _safe_int(r.get('AdPos')),
            tot_ads       = _safe_int(r.get('TotAds')),
            brk_no        = _safe_int(r.get('BrkNo')),
            pos_in_brk    = _safe_int(r.get('PosinBrk')),
            ads_in_brk    = _safe_int(r.get('AdsinBrk')),
            lng           = _safe_str(r.get('Lng', '')),
            cost          = _safe_decimal(r.get('Cost')),
            day           = _safe_str(r.get('Day', '')),
        )

    print(f"[_parse_lmrb_rows] df rows={len(df)}  unique dedup_keys={len(rows_by_key)}")
    if not rows_by_key:
        print("[_parse_lmrb_rows] WARNING: no valid rows to insert (check column names and key fields)")
        return

    # ── Handle existing duplicates (unlock linked ScheduleRows first) ──────────
    existing = LMRBRow.objects.filter(dedup_key__in=rows_by_key.keys())
    existing_count = existing.count()
    print(f"[_parse_lmrb_rows] existing duplicate rows found={existing_count}")
    sch_row_ids_to_unlock = []
    for old in existing:
        if old.is_matched and old.matched_schedule_id:
            sch_row_ids_to_unlock.append(old.matched_schedule_id)

    print(f"[_parse_lmrb_rows] ScheduleRows to unlock={len(sch_row_ids_to_unlock)}")
    if sch_row_ids_to_unlock:
        from django.utils import timezone
        ScheduleRow.objects.filter(id__in=sch_row_ids_to_unlock).update(
            is_matched=False,
            matched_lmrb=None,
            matched_at=None,
        )
        print(f"[_parse_lmrb_rows] unlocked {len(sch_row_ids_to_unlock)} ScheduleRow(s)")

    # Delete old rows (new ones will be inserted below)
    deleted_count = existing.count()
    existing.delete()
    print(f"[_parse_lmrb_rows] deleted {deleted_count} old LMRBRow(s)")

    # ── Bulk-create new rows ──────────────────────────────────────────────────
    new_rows = list(rows_by_key.values())
    LMRBRow.objects.bulk_create(new_rows, batch_size=500)
    print(f"[_parse_lmrb_rows] inserted {len(new_rows)} new LMRBRow(s)")


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

    # Delete the shared file once (all records in the group share the same file)
    if first.file:
        print(f"[monitoring_delete_group] deleting shared file: {first.file.name}")
        first.file.delete(save=False)

    group_qs.delete()
    print(f"[monitoring_delete_group] deleted {count} MonitoringData record(s) successfully")
    messages.success(request, f'Deleted {count} channel record(s): {", ".join(channels)}.')
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
    # MatchResult and ScheduleRow already imported at top level

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
            'full':     list(qs.select_related('lmrb_row').order_by('scheduled_date', 'brand')),
            'matched':  list(qs.filter(status='matched').select_related('lmrb_row').order_by('scheduled_date', 'brand')),
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

        # Sponsorship rows for the separate sponsorship tab
        sponsorship_rows = list(ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month, ad_type='SPONSORSHIP',
        ).order_by('date', 'start_time'))

        # Sponsorship brand breakdown
        sponsorship_brand_summary = []
        for br in ScheduleRow.objects.filter(
            account_id=account_id, channel=channel, month=month, ad_type='SPONSORSHIP',
        ).values_list('brand', flat=True).distinct().order_by('brand'):
            sq = ScheduleRow.objects.filter(
                account_id=account_id, channel=channel, month=month,
                ad_type='SPONSORSHIP', brand=br,
            )
            st = sq.count()
            sm = sq.filter(is_matched=True).count()
            sponsorship_brand_summary.append({
                'brand': br,
                'total': st,
                'matched': sm,
                'not_matched': st - sm,
            })

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
        # channel__iexact: LMRB file may store channel name in different case
        # (e.g. "SIRASA TV" vs "Sirasa TV"). Use case-insensitive filter so data
        # is never silently missed.
        lmrb_qs = LMRBRow.objects.filter(account_id=account_id, channel__iexact=channel)
        if sch_dates['d_min']:
            lmrb_qs = lmrb_qs.filter(date__gte=sch_dates['d_min'])
        if sch_dates['d_max']:
            lmrb_qs = lmrb_qs.filter(date__lte=sch_dates['d_max'])
        print(f"[monitoring_dashboard] lmrb_qs count={lmrb_qs.count()} "
              f"(account={account_id}, channel='{channel}', "
              f"date_range={sch_dates['d_min']} → {sch_dates['d_max']})")
        lmrb_chart = list(
            lmrb_qs.values('advt_theme', 'duration')
            .annotate(count=Count('id'))
            .order_by('advt_theme', 'duration')
        )

        # ── Matched LMRB rows (for LMRB Matched tab) ─────────────────────────
        lmrb_matched_rows = list(
            lmrb_qs.filter(is_matched=True).order_by('date', 'advt_time')
        )
        print(f"[monitoring_dashboard] lmrb_matched={len(lmrb_matched_rows)}  lmrb_unmatched={lmrb_qs.filter(is_matched=False).count()}")
        # ── Unmatched LMRB rows (for LMRB Unmatched tab) ─────────────────────
        lmrb_unmatched_rows = list(
            lmrb_qs.filter(is_matched=False).order_by('date', 'advt_time')
        )

    return render(request, 'monitoring/dashboard.html', {
        'accounts':                  account_qs,
        'selected_account':          selected_account,
        'account_id':                account_id,
        'channels':                  channels,
        'months':                    months,
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
        'lmrb_matched_rows':         lmrb_matched_rows,
        'lmrb_unmatched_rows':       lmrb_unmatched_rows,
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

    _ci_rename('Channel',   ['Station', 'CHANNEL', 'channel'])
    _ci_rename('Date',      ['Aired Date', 'Prg Date', 'aired_date', 'AiredDate', 'Prg_Date'])
    _ci_rename('Programme', ['Program', 'Prg Name', 'PrgName', 'programme'])
    _ci_rename('TC_Theme',  ['Advt_Theme', 'Advt_theme', 'Theme', 'theme',
                              'Product', 'Description', 'Ad Name', 'AdName', 'Ad_Name'])
    _ci_rename('Duration',  ['Dur', 'Seconds', 'Ad Dur', 'Duration_Sec'])
    _ci_rename('Aired_Time',['Advt_Time', 'Advt_time', 'advt_Time', 'Time',
                              'Aired Time', 'Ad Start', 'AdTime', 'AiredTime'])

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

    account_id = request.POST.get('account_id') or request.GET.get('account_id', '')
    channel    = request.POST.get('channel')    or request.GET.get('channel', '')
    month      = request.POST.get('month')      or request.GET.get('month', '')
    mode       = request.POST.get('mode', 'smart')

    if not (account_id and channel and month):
        messages.error(request, 'Account, channel and month are required.')
        return redirect('/dashboard/tc/')

    if not _account_access(request.user, account_id):
        messages.error(request, 'Access denied.')
        return redirect('/dashboard/tc/')

    try:
        result = reconcile_tc(account_id, channel, month, mode=mode)
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

    return redirect(f'/dashboard/summary/?account_id={account_id}&channel={channel}&month={month}')


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

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')

    selected_account = None
    channels = []
    months   = []
    rows     = []

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

        sch_qs = (
            ScheduleRow.objects
            .filter(account_id=account_id, channel=channel, month=month)
            .select_related('matched_lmrb')
            .prefetch_related('tc_matches')
            .order_by('date', 'start_time', 'brand')
        )

        for sr in sch_qs:
            lmrb = sr.matched_lmrb
            tc   = sr.tc_matches.filter(is_schedule_matched=True).first()

            # Determine row status for colour-coding
            if tc and tc.is_lmrb_confirmed:
                status = 'aired'          # confirmed by both TC and LMRB
            elif tc and not tc.is_lmrb_confirmed:
                status = 'tc_only'        # TC says aired but LMRB doesn't confirm
            elif lmrb:
                status = 'lmrb_only'      # LMRB found a match but no TC record
            else:
                status = 'not_aired'      # neither source confirms it

            rows.append({
                'brand':          sr.brand,
                'ad_type':        sr.ad_type,
                'duration':       sr.duration,
                # Plan (Schedule)
                'plan_date':      sr.date,
                'plan_programme': sr.programme,
                'plan_start':     sr.start_time,
                'plan_end':       sr.end_time,
                # LMRB (3rd-party monitoring)
                'lmrb_date':      lmrb.date       if lmrb else None,
                'lmrb_programme': lmrb.program    if lmrb else '',
                'lmrb_time':      lmrb.advt_time  if lmrb else '',
                'lmrb_theme':     lmrb.advt_theme if lmrb else '',
                # TC (channel certificate)
                'tc_date':        tc.date          if tc else None,
                'tc_programme':   tc.programme     if tc else '',
                'tc_time':        tc.aired_time    if tc else '',
                'tc_theme':       tc.tc_theme      if tc else '',
                # Status flags
                'has_lmrb':            lmrb is not None,
                'has_tc':              tc is not None,
                'tc_lmrb_confirmed':   tc.is_lmrb_confirmed if tc else False,
                'status':              status,
            })

    return render(request, 'tc/detail.html', {
        'accounts':          account_qs,
        'selected_account':  selected_account,
        'account_id':        account_id,
        'channels':          channels,
        'months':            months,
        'channel':           channel,
        'month':             month,
        'rows':              rows,
        'total':             len(rows),
        'n_aired':           sum(1 for r in rows if r['status'] == 'aired'),
        'n_tc_only':         sum(1 for r in rows if r['status'] == 'tc_only'),
        'n_lmrb_only':       sum(1 for r in rows if r['status'] == 'lmrb_only'),
        'n_not_aired':       sum(1 for r in rows if r['status'] == 'not_aired'),
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

    account_id = request.GET.get('account_id', '')
    channel    = request.GET.get('channel', '')
    month      = request.GET.get('month', '')

    # Save metadata if POST
    if request.method == 'POST':
        account_id = request.POST.get('account_id', '').strip()
        channel    = request.POST.get('channel', '').strip()
        month      = request.POST.get('month', '').strip()

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
        return redirect(f'/dashboard/summary/?account_id={account_id}&channel={channel}&month={month}')

    # Dropdown data
    channels = []
    months   = []
    selected_account = None
    summary_data = None
    meta         = None
    schedule_obj = None
    tc_report    = None

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

        try:
            summary_data = build_summary_data(account_id, channel, month)
        except Exception as e:
            messages.warning(request, f'Could not build summary: {e}')

        meta = SummaryReportMeta.objects.filter(
            account_id=account_id, channel=channel, month=month
        ).first()

        schedule_obj = Schedule.objects.filter(
            account_id=account_id, channel=channel, month=month
        ).order_by('-uploaded_at').first()

        tc_report = TransmissionReport.objects.filter(
            account_id=account_id, channel=channel, month=month
        ).order_by('-uploaded_at').first()

    return render(request, 'summary/report.html', {
        'accounts':        account_qs,
        'selected_account': selected_account,
        'account_id':      account_id,
        'channels':        channels,
        'months':          months,
        'channel':         channel,
        'month':           month,
        'summary_data':    summary_data,
        'meta':            meta,
        'schedule_obj':    schedule_obj,
        'tc_report':       tc_report,
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

    # Column widths
    ws.column_dimensions['A'].width = 36
    ws.column_dimensions['B'].width = 8
    ws.column_dimensions['C'].width = 10
    ws.column_dimensions['D'].width = 10
    ws.column_dimensions['E'].width = 10
    ws.column_dimensions['F'].width = 10
    ws.column_dimensions['G'].width = 12
    ws.column_dimensions['H'].width = 14

    row = 1

    # ── Title ─────────────────────────────────────────────────────────────────
    ws.merge_cells(f'A{row}:H{row}')
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
        ws.merge_cells(f'A{row}:D{row}')
        c = ws.cell(row, 1, addr); c.font = norm10; c.alignment = left
        ws.merge_cells(f'E{row}:F{row}')
        c = ws.cell(row, 5, label + '  :'); c.font = bold10; c.alignment = right
        ws.merge_cells(f'G{row}:H{row}')
        c = ws.cell(row, 7, value); c.font = norm10; c.alignment = left
        row += 1

    row += 1  # blank

    # ── Column headers ────────────────────────────────────────────────────────
    headers = ['PRODUCT', 'DUR', 'PLANNED', 'AIRED', 'MISSED', 'EXTRA', '3RD PARTY', 'Avg 30s']
    for col_i, h in enumerate(headers, start=1):
        c = ws.cell(row, col_i, h)
        c.font = hdr_font; c.fill = NAVY; c.alignment = centre
        c.border = border()
    ws.row_dimensions[row].height = 20
    row += 1

    # ── Commercial Benefits section ───────────────────────────────────────────
    if data['commercial']:
        ws.merge_cells(f'A{row}:H{row}')
        c = ws.cell(row, 1, 'Commercial Benefits'); c.font = bold10; c.fill = LBLUE
        c.alignment = left
        row += 1

        for item in data['commercial']:
            vals = [item['product'], item['dur'], item['planned'], item['aired'],
                    item['missed'], item['extra'], item['third_party'], item['avg_30']]
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
                      t['extra'], t['third_party'], t['avg_30']]
        for col_i, v in enumerate(total_vals, start=1):
            c = ws.cell(row, col_i, v)
            c.font = bold10; c.fill = DGRAY; c.alignment = centre if col_i > 1 else left
            c.border = border()
        row += 1

        # Total avg 30s (whole row)
        ws.merge_cells(f'A{row}:G{row}')
        ws.cell(row, 1, '').fill = LGRAY
        c = ws.cell(row, 8, round(t['avg_30'] * 2, 2))
        c.font = bold11; c.fill = GOLD; c.alignment = centre; c.border = border()
        row += 1

    row += 1  # blank

    # ── Sponsorship Benefits section ──────────────────────────────────────────
    if data['sponsorship']:
        ws.merge_cells(f'A{row}:H{row}')
        c = ws.cell(row, 1, 'Sponsorship Benefits'); c.font = bold11; c.fill = GOLD
        c.alignment = left
        row += 1

        for section in data['sponsorship']:
            # Programme heading
            ws.merge_cells(f'A{row}:H{row}')
            c = ws.cell(row, 1, section['programme'].upper())
            c.font = bold10; c.fill = LBLUE; c.alignment = left
            row += 1

            for item in section['rows']:
                vals = [item['product'], item['dur'], item['planned'], item['aired'],
                        item['missed'], item['extra'], item['third_party'], item['avg_30']]
                for col_i, v in enumerate(vals, start=1):
                    c = ws.cell(row, col_i, v)
                    c.font = norm10
                    c.alignment = centre if col_i > 1 else left
                    c.border = border()
                row += 1

            # Subtotal
            st = section['subtotal']
            st_vals = ['Grand Total', '', st['planned'], st['aired'], st['missed'],
                       st['extra'], st['third_party'], st['avg_30']]
            for col_i, v in enumerate(st_vals, start=1):
                c = ws.cell(row, col_i, v)
                c.font = bold10; c.fill = DGRAY; c.alignment = centre if col_i > 1 else left
                c.border = border()
            row += 1
            row += 1  # blank between programmes

        # Sponsorship Grand Total
        st = data['sponsorship_total']
        ws.merge_cells(f'A{row}:H{row}')
        c = ws.cell(row, 1, 'SPONSORSHIP GRAND TOTAL'); c.font = bold11; c.fill = GOLD
        c.alignment = left
        row += 1
        total_vals = ['Grand Total', '', st['planned'], st['aired'], st['missed'],
                      st['extra'], st['third_party'], st['avg_30']]
        for col_i, v in enumerate(total_vals, start=1):
            c = ws.cell(row, col_i, v)
            c.font = bold10; c.fill = DGRAY; c.alignment = centre if col_i > 1 else left
            c.border = border()
        row += 1

    row += 1  # blank

    # ── Notes ─────────────────────────────────────────────────────────────────
    ws.merge_cells(f'A{row}:H{row}')
    ws.cell(row, 1, 'NOTE:').font = bold10
    row += 1
    notes_text = meta.notes if meta and meta.notes else 'Channel transmission attached\nSponsorship Benefits confirmation email attached'
    for line in notes_text.split('\n'):
        ws.merge_cells(f'A{row}:H{row}')
        c = ws.cell(row, 1, line.strip()); c.font = norm10
        row += 1

    row += 2  # blank

    # ── Signatures ────────────────────────────────────────────────────────────
    sig_labels = ['PREPARED BY', 'CHECKED BY', 'AUTHORISED BY']
    sig_values = [
        meta.prepared_by if meta else '',
        meta.checked_by  if meta else '',
        meta.authorised_by if meta else '',
    ]
    for col_i, (lbl, val) in enumerate(zip(sig_labels, sig_values)):
        start_col = col_i * 3 + 1
        end_col   = start_col + 2
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
    """Download full Reconciliation PDF: Summary page + Matched LMRB report."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
        HRFlowable, PageBreak,
    )
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
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

    account = get_object_or_404(Account, id=account_id)
    data    = build_summary_data(account_id, channel, month)
    meta    = SummaryReportMeta.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).first()
    sched   = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).order_by('-uploaded_at').first()
    estimate_no = sched.schedule_number if sched else ''

    # ── ReportLab setup ───────────────────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=1.5*cm, rightMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm,
    )

    NAVY  = colors.HexColor('#0f2340')
    BLUE  = colors.HexColor('#2563eb')
    LGRAY = colors.HexColor('#f8fafc')
    MGRAY = colors.HexColor('#e2e8f0')
    DGRAY = colors.HexColor('#e2e8f0')
    GOLD  = colors.HexColor('#fef9c3')
    LBLUE = colors.HexColor('#dbeafe')
    RED   = colors.HexColor('#fee2e2')
    GREEN = colors.HexColor('#dcfce7')

    styles = getSampleStyleSheet()
    h_title = ParagraphStyle('title', fontSize=14, textColor=NAVY,
                              fontName='Helvetica-Bold', spaceAfter=4)
    h_sub   = ParagraphStyle('sub',   fontSize=9,  textColor=colors.HexColor('#475569'),
                              fontName='Helvetica', spaceAfter=2)
    h_cell  = ParagraphStyle('cell',  fontSize=7.5, fontName='Helvetica')
    h_hdr   = ParagraphStyle('hdr',   fontSize=7.5, fontName='Helvetica-Bold',
                              textColor=colors.white)
    h_sect  = ParagraphStyle('sect',  fontSize=9,  fontName='Helvetica-Bold',
                              textColor=NAVY)

    story = []

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 1: SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════

    story.append(Paragraph('RECONCILIATION SUMMARY REPORT', h_title))
    story.append(Paragraph(
        f'Account: {account.name}  |  Channel: {channel}  |  Month: {month}  |  '
        f'Estimate No: {estimate_no}',
        h_sub,
    ))
    story.append(Paragraph(
        f'Generated: {datetime.now().strftime("%d %B %Y %H:%M")}',
        h_sub,
    ))
    story.append(HRFlowable(width='100%', thickness=1, color=BLUE, spaceAfter=8))

    # Column headers for summary tables
    SUM_HEADERS = ['Product', 'Dur', 'Planned', 'Aired', 'Missed', 'Extra', '3rd Party', 'Avg 30s']
    SUM_WIDTHS  = [5*cm, 1.5*cm, 1.8*cm, 1.8*cm, 1.8*cm, 1.8*cm, 2*cm, 2*cm]

    def _summary_table(rows, total_row):
        """Build a ReportLab Table for summary data rows."""
        tdata = [[Paragraph(h, h_hdr) for h in SUM_HEADERS]]
        for r in rows:
            tdata.append([
                Paragraph(str(r.get('product', '')), h_cell),
                Paragraph(str(r.get('dur') or '—'), h_cell),
                Paragraph(str(r.get('planned', 0)), h_cell),
                Paragraph(str(r.get('aired', 0)), h_cell),
                Paragraph(str(r.get('missed', 0)), h_cell),
                Paragraph(str(r.get('extra', 0)), h_cell),
                Paragraph(str(r.get('third_party', 0)), h_cell),
                Paragraph(str(r.get('avg_30', 0)), h_cell),
            ])
        # Total row
        t = total_row
        tdata.append([
            Paragraph('Grand Total', ParagraphStyle('gt', fontSize=7.5, fontName='Helvetica-Bold')),
            Paragraph('', h_cell),
            Paragraph(str(t.get('planned', 0)), h_cell),
            Paragraph(str(t.get('aired', 0)), h_cell),
            Paragraph(str(t.get('missed', 0)), h_cell),
            Paragraph(str(t.get('extra', 0)), h_cell),
            Paragraph(str(t.get('third_party', 0)), h_cell),
            Paragraph(str(t.get('avg_30', 0)), h_cell),
        ])
        tbl = Table(tdata, colWidths=SUM_WIDTHS, repeatRows=1)
        tbl.setStyle(TableStyle([
            ('BACKGROUND',  (0, 0), (-1, 0),  NAVY),
            ('TEXTCOLOR',   (0, 0), (-1, 0),  colors.white),
            ('FONTNAME',    (0, 0), (-1, 0),  'Helvetica-Bold'),
            ('FONTSIZE',    (0, 0), (-1, -1), 7.5),
            ('ROWBACKGROUNDS', (0, 1), (-1, -2), [LGRAY, colors.white]),
            ('BACKGROUND',  (0, -1), (-1, -1), DGRAY),
            ('FONTNAME',    (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('VALIGN',      (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',  (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('GRID',        (0, 0), (-1, -1), 0.3, MGRAY),
            ('LINEBELOW',   (0, 0), (-1, 0),  1,   BLUE),
        ]))
        return tbl

    # Commercial Benefits
    if data.get('commercial'):
        story.append(Paragraph('Commercial Benefits', h_sect))
        story.append(Spacer(1, 0.2*cm))
        story.append(_summary_table(data['commercial'], data['commercial_total']))
        story.append(Spacer(1, 0.4*cm))

    # Sponsorship Benefits
    if data.get('sponsorship'):
        story.append(Paragraph('Sponsorship Benefits', h_sect))
        story.append(Spacer(1, 0.2*cm))
        for section in data['sponsorship']:
            prog_label = ParagraphStyle('prog', fontSize=8, fontName='Helvetica-Bold',
                                        textColor=BLUE)
            story.append(Paragraph(section['programme'].upper(), prog_label))
            story.append(Spacer(1, 0.1*cm))
            story.append(_summary_table(section['rows'], section['subtotal']))
            story.append(Spacer(1, 0.3*cm))

    # Notes
    if meta and meta.notes:
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph('NOTE:', h_sect))
        for line in meta.notes.splitlines():
            story.append(Paragraph(line, h_sub))

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 2+: MATCHED LMRB REPORT (full LMRB columns)
    # ═══════════════════════════════════════════════════════════════════════════

    story.append(PageBreak())

    story.append(Paragraph('Matched LMRB Report', h_title))
    story.append(Paragraph(
        f'Account: {account.name}  |  Channel: {channel}  |  Month: {month}  |  '
        f'All LMRB entries confirmed against TC',
        h_sub,
    ))
    story.append(HRFlowable(width='100%', thickness=1, color=BLUE, spaceAfter=8))

    # Fetch all LMRBRows confirmed via TC for this scope
    from django.db.models import Min, Max
    sch_dates = Schedule.objects.filter(
        account_id=account_id, channel=channel, month=month
    ).aggregate(d_min=Min('start_date'), d_max=Max('end_date'))
    date_min = sch_dates.get('d_min')
    date_max = sch_dates.get('d_max')

    lmrb_qs = LMRBRow.objects.filter(
        account_id=account_id, channel=channel,
    )
    if date_min and date_max:
        lmrb_qs = lmrb_qs.filter(date__range=(date_min, date_max))
    # Only rows confirmed by TC reconciliation
    lmrb_qs = lmrb_qs.filter(tc_confirmations__isnull=False).distinct().order_by('date', 'advt_time')

    LMRB_HEADERS = [
        'Date', 'Time', 'Theme', 'Dur', 'Programme', 'Prog Time',
        'Advertiser', 'Product', 'Ad Pos', 'Brk No', 'Pos in Brk',
        'Ads in Brk', 'Day', 'Cost',
    ]
    LMRB_WIDTHS = [
        1.8*cm, 1.6*cm, 4*cm, 1.2*cm, 3*cm, 1.6*cm,
        3*cm, 2.5*cm, 1.2*cm, 1.2*cm, 1.5*cm,
        1.5*cm, 1.2*cm, 1.8*cm,
    ]

    if lmrb_qs.exists():
        lmrb_data = [[Paragraph(h, h_hdr) for h in LMRB_HEADERS]]
        for lr in lmrb_qs:
            cost_str = f'{lr.cost:,.2f}' if lr.cost is not None else '—'
            lmrb_data.append([
                Paragraph(str(lr.date), h_cell),
                Paragraph(str(lr.advt_time or '—'), h_cell),
                Paragraph(str(lr.advt_theme or '—'), h_cell),
                Paragraph(str(lr.duration or '—'), h_cell),
                Paragraph(str(lr.program or '—'), h_cell),
                Paragraph(str(lr.prog_time or '—'), h_cell),
                Paragraph(str(lr.advertiser or '—'), h_cell),
                Paragraph(str(lr.product or '—'), h_cell),
                Paragraph(str(lr.ad_pos if lr.ad_pos is not None else '—'), h_cell),
                Paragraph(str(lr.brk_no if lr.brk_no is not None else '—'), h_cell),
                Paragraph(str(lr.pos_in_brk if lr.pos_in_brk is not None else '—'), h_cell),
                Paragraph(str(lr.ads_in_brk if lr.ads_in_brk is not None else '—'), h_cell),
                Paragraph(str(lr.day or '—'), h_cell),
                Paragraph(cost_str, h_cell),
            ])

        lmrb_tbl = Table(lmrb_data, colWidths=LMRB_WIDTHS, repeatRows=1)
        lmrb_tbl.setStyle(TableStyle([
            ('BACKGROUND',  (0, 0), (-1, 0),  NAVY),
            ('TEXTCOLOR',   (0, 0), (-1, 0),  colors.white),
            ('FONTNAME',    (0, 0), (-1, 0),  'Helvetica-Bold'),
            ('FONTSIZE',    (0, 0), (-1, -1), 7),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [LGRAY, colors.white]),
            ('VALIGN',      (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING',  (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ('GRID',        (0, 0), (-1, -1), 0.3, MGRAY),
            ('LINEBELOW',   (0, 0), (-1, 0),  1,   BLUE),
        ]))
        story.append(lmrb_tbl)
    else:
        story.append(Paragraph('No matched LMRB rows found for this scope.', styles['Normal']))

    # ── Build response ─────────────────────────────────────────────────────────
    doc.build(story)
    buf.seek(0)
    fname = f'Reconciliation_{account.name}_{channel}_{month}.pdf'.replace(' ', '_')
    response = HttpResponse(buf.read(), content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    return response
