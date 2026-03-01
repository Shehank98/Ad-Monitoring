import pandas as pd
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from accounts.decorators import role_required
from accounts.models import User
from accounts.views import create_user, edit_user, user_list

from .forms import AccountForm, ChannelForm, MonitoringUploadForm, ScheduleUploadForm
from .models import Account, Channel, MonitoringData, Schedule


# ── Dashboard ─────────────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    user  = request.user
    role  = user.role
    ctx   = {'user': user}

    if role in ('super_admin', 'admin'):
        ctx['total_users']    = User.objects.count()
        ctx['active_users']   = User.objects.filter(is_active=True).count()
        ctx['total_schedules'] = Schedule.objects.count()
        ctx['total_mon']      = MonitoringData.objects.count()
        ctx['recent_schedules'] = Schedule.objects.select_related('account', 'uploaded_by')[:5]
        ctx['recent_mon']     = MonitoringData.objects.select_related('uploaded_by')[:5]

    elif role == 'team_head':
        my_accounts = user.accounts.all()
        sch = Schedule.objects.filter(account__in=my_accounts).select_related('account')
        ctx['my_accounts']   = my_accounts
        ctx['schedules']     = sch[:5]
        ctx['schedule_count'] = sch.count()
        ctx['mon_count']     = MonitoringData.objects.count()

    elif role == 'planner':
        my_accounts = user.accounts.all()
        sch = Schedule.objects.filter(uploaded_by=user).select_related('account')
        ctx['my_accounts']    = my_accounts
        ctx['my_schedules']   = sch[:5]
        ctx['schedule_count'] = sch.count()

    elif role == 'operations':
        mon = MonitoringData.objects.filter(uploaded_by=user)
        ctx['my_uploads']  = mon[:5]
        ctx['upload_count'] = mon.count()
        ctx['total_mon']   = MonitoringData.objects.count()

    return render(request, 'dashboard/home.html', ctx)


# ── Account management (super_admin / admin) ──────────────────────────────────

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


# ── Channel management (super_admin / admin) ──────────────────────────────────

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


# ── Schedules ──────────────────────────────────────────────────────────────────

@login_required
def schedule_list(request):
    user = request.user
    qs   = Schedule.objects.select_related('account', 'uploaded_by')

    if user.role == 'planner':
        qs = qs.filter(uploaded_by=user)
    elif user.role == 'team_head':
        qs = qs.filter(account__in=user.accounts.all())
    # super_admin, admin, operations see all

    # Filters from GET
    account_id = request.GET.get('account')
    channel    = request.GET.get('channel', '').strip()
    month      = request.GET.get('month', '').strip()
    if account_id:
        qs = qs.filter(account_id=account_id)
    if channel:
        qs = qs.filter(channel__icontains=channel)
    if month:
        qs = qs.filter(month__icontains=month)

    accounts = Account.objects.all()
    return render(request, 'schedules/list.html', {
        'schedules': qs,
        'accounts':  accounts,
        'filters':   {'account': account_id, 'channel': channel, 'month': month},
    })


@login_required
@role_required(['planner', 'super_admin', 'admin'])
def schedule_upload(request):
    user = request.user
    form = ScheduleUploadForm()

    # Restrict planners to their accounts
    if user.role == 'planner':
        form.fields['account'].queryset = user.accounts.all()

    if request.method == 'POST':
        form = ScheduleUploadForm(request.POST, request.FILES)
        if user.role == 'planner':
            form.fields['account'].queryset = user.accounts.all()

        if form.is_valid():
            excel_file = request.FILES['file']
            try:
                df        = pd.read_excel(excel_file)
                row_count = len(df)
            except Exception as e:
                messages.error(request, f'Cannot read Excel file: {e}')
                return render(request, 'schedules/upload.html', {'form': form})

            excel_file.seek(0)
            schedule = Schedule(
                account         = form.cleaned_data['account'],
                channel         = form.cleaned_data['channel'].name,
                month           = form.cleaned_data['month'],
                schedule_number = form.cleaned_data['schedule_number'],
                original_filename = excel_file.name,
                row_count       = row_count,
                uploaded_by     = user,
            )
            schedule.file.save(excel_file.name, excel_file)
            schedule.save()
            messages.success(request,
                f'Schedule #{schedule.schedule_number} for {schedule.account} '
                f'uploaded successfully ({row_count:,} rows).')
            return redirect('/dashboard/schedules/')

    return render(request, 'schedules/upload.html', {'form': form})


@login_required
def schedule_delete(request, pk):
    schedule = get_object_or_404(Schedule, pk=pk)
    user     = request.user
    if user.role not in ('super_admin', 'admin') and schedule.uploaded_by != user:
        messages.error(request, 'You can only delete your own schedules.')
        return redirect('/dashboard/schedules/')
    schedule.file.delete(save=False)
    schedule.delete()
    messages.success(request, 'Schedule deleted.')
    return redirect('/dashboard/schedules/')


# ── Monitoring data ───────────────────────────────────────────────────────────

@login_required
def monitoring_list(request):
    qs       = MonitoringData.objects.select_related('uploaded_by')
    dtype    = request.GET.get('type', '')
    channel  = request.GET.get('channel', '').strip()
    if dtype:
        qs = qs.filter(data_type=dtype)
    if channel:
        qs = qs.filter(channel__icontains=channel)
    return render(request, 'monitoring/list.html', {
        'data_list': qs,
        'filters':   {'type': dtype, 'channel': channel},
    })


@login_required
@role_required(['operations', 'super_admin', 'admin'])
def monitoring_upload(request):
    form = MonitoringUploadForm()

    if request.method == 'POST':
        form = MonitoringUploadForm(request.POST, request.FILES)
        if form.is_valid():
            excel_file = request.FILES['file']
            try:
                df        = pd.read_excel(excel_file)
                row_count = len(df)
            except Exception as e:
                messages.error(request, f'Cannot read Excel file: {e}')
                return render(request, 'monitoring/upload.html', {'form': form})

            excel_file.seek(0)
            mon = MonitoringData(
                data_type       = form.cleaned_data['data_type'],
                channel         = form.cleaned_data['channel'].name,
                start_date      = form.cleaned_data['start_date'],
                end_date        = form.cleaned_data['end_date'],
                original_filename = excel_file.name,
                row_count       = row_count,
                uploaded_by     = request.user,
            )
            mon.file.save(excel_file.name, excel_file)
            mon.save()
            messages.success(request,
                f'{mon.get_data_type_display()} data for {mon.channel} '
                f'uploaded ({row_count:,} rows).')
            return redirect('/dashboard/monitoring/')

    return render(request, 'monitoring/upload.html', {'form': form})


@login_required
@role_required(['operations', 'super_admin', 'admin'])
def monitoring_delete(request, pk):
    mon = get_object_or_404(MonitoringData, pk=pk)
    if request.user.role not in ('super_admin', 'admin') and mon.uploaded_by != request.user:
        messages.error(request, 'You can only delete your own uploads.')
        return redirect('/dashboard/monitoring/')
    mon.file.delete(save=False)
    mon.delete()
    messages.success(request, 'Monitoring data deleted.')
    return redirect('/dashboard/monitoring/')
