import os
import secrets as secrets_mod
import string

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Account, AuditLog
from .decorators import role_required
from .forms import ChangePasswordForm, CreateUserForm, LoginForm
from .models import User


def _branding_url(asset_type: str) -> str:
    """Return media URL for a branding asset, or empty string if not uploaded."""
    branding_dir = os.path.join(settings.MEDIA_ROOT, 'branding')
    for ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'):
        path = os.path.join(branding_dir, f'{asset_type}{ext}')
        if os.path.exists(path):
            return settings.MEDIA_URL + f'branding/{asset_type}{ext}'
    return ''


def _login_ctx(form):
    from datetime import date as _date
    return {
        'form':       form,
        'logo_url':   _branding_url('logo'),
        'tartan_url': _branding_url('tartan'),
        'year':       _date.today().year,
    }


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect('/dashboard/')

    if request.method == 'POST':
        form = LoginForm(request.POST)
        if form.is_valid():
            email    = form.cleaned_data['email'].strip().lower()
            password = form.cleaned_data['password']

            domain = settings.ALLOWED_EMAIL_DOMAIN
            if domain and not email.endswith(f'@{domain}'):
                messages.error(request, f'Only @{domain} email addresses are allowed.')
                return render(request, 'accounts/login.html', _login_ctx(form))

            user = authenticate(request, email=email, password=password)
            if user is None:
                messages.error(request, 'Invalid email or password.')
            elif not user.is_active:
                messages.error(request, 'Your account has been deactivated. Contact your administrator.')
            else:
                login(request, user)
                if user.must_change_password:
                    messages.info(request, 'Please change your temporary password to continue.')
                    return redirect('/auth/change-password/')
                next_url = request.GET.get('next', '')
                if next_url and next_url.startswith('/'):
                    return redirect(next_url)
                if user.role == 'channel_officer':
                    return redirect('/dashboard/tc/upload/')
                return redirect('/dashboard/')
    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', _login_ctx(form))


def logout_view(request):
    logout(request)
    return redirect('/auth/login/')


@login_required
def change_password_view(request):
    if request.method == 'POST':
        form = ChangePasswordForm(request.POST)
        if form.is_valid():
            user = request.user
            if not user.check_password(form.cleaned_data['current_password']):
                messages.error(request, 'Current password is incorrect.')
            else:
                user.set_password(form.cleaned_data['new_password'])
                user.must_change_password = False
                user.save()
                login(request, user)
                messages.success(request, 'Password updated successfully!')
                return redirect('/dashboard/')
    else:
        form = ChangePasswordForm()

    return render(request, 'accounts/change_password.html', {'form': form})


# ── User management ────────────────────────────────────────────────────────────

@login_required
@role_required(['super_admin', 'admin', 'team_head'])
def user_list(request):
    me = request.user
    qs = User.objects.select_related('created_by').prefetch_related('accounts')

    if me.role == 'super_admin':
        pass  # sees everyone
    elif me.role == 'admin':
        qs = qs.exclude(role='super_admin')
    elif me.role == 'team_head':
        # Team head sees only planner/operations users assigned to their accounts
        my_account_ids = me.accounts.values_list('id', flat=True)
        qs = qs.filter(
            role__in=['planner', 'operations'],
            accounts__in=my_account_ids,
        ).distinct()

    # C2: Activity stats — upload counts and inactivity flags
    from django.db.models import Count as _Count, Max as _Max
    from django.utils import timezone as _tz
    from datetime import timedelta as _td
    from core.models import Schedule as _Sch, MonitoringData as _Mon

    _inactive_cutoff = _tz.now() - _td(days=30)

    _sch_counts = dict(
        _Sch.objects.values('uploaded_by_id').annotate(n=_Count('id')).values_list('uploaded_by_id', 'n')
    )
    _mon_counts = dict(
        _Mon.objects.values('uploaded_by_id').annotate(n=_Count('id')).values_list('uploaded_by_id', 'n')
    )

    users_list = list(qs)
    for u in users_list:
        u.sch_upload_count = _sch_counts.get(u.id, 0)
        u.mon_upload_count = _mon_counts.get(u.id, 0)
        u.is_inactive = (u.last_login is not None and u.last_login < _inactive_cutoff)
        u.never_logged_in = u.last_login is None

    can_create = bool(me.creatable_roles())
    return render(request, 'admin_panel/users.html', {
        'users':      users_list,
        'can_create': can_create,
    })


@login_required
@role_required(['super_admin', 'admin', 'team_head'])
def create_user(request):
    me            = request.user
    allowed_roles = me.creatable_roles()

    # team_head: restrict account choices to their own accounts
    if me.role == 'team_head':
        account_qs = me.accounts.all()
    else:
        account_qs = Account.objects.all()

    if request.method == 'POST':
        form = CreateUserForm(request.POST, allowed_roles=allowed_roles, account_qs=account_qs)
        if form.is_valid():
            email  = form.cleaned_data['email'].strip().lower()
            domain = settings.ALLOWED_EMAIL_DOMAIN
            if domain and not email.endswith(f'@{domain}'):
                messages.error(request, f'Email must end with @{domain}.')
            elif User.objects.filter(email=email).exists():
                messages.error(request, 'An account with this email already exists.')
            else:
                new_user = User.objects.create_user(
                    email=email,
                    name=form.cleaned_data['name'],
                    password=form.cleaned_data['password'],
                    role=form.cleaned_data['role'],
                    whatsapp_number=form.cleaned_data.get('whatsapp_number', '').strip(),
                    created_by=me,
                    must_change_password=True,
                )
                if form.cleaned_data.get('accounts'):
                    new_user.accounts.set(form.cleaned_data['accounts'])
                messages.success(request, f'User {new_user.name} created successfully.')
                ip = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                      or request.META.get('REMOTE_ADDR'))
                AuditLog.objects.create(
                    user=me, action='user_create',
                    detail=f'Created {new_user.name} ({new_user.email}) as {new_user.role}.',
                    ip=ip or None,
                )

                # Send WhatsApp welcome + credentials if number provided
                wa = form.cleaned_data.get('whatsapp_number', '').strip()
                if wa:
                    try:
                        from core.whatsapp import notify_new_user_created
                        from core.models import get_setting
                        base_url = get_setting('whatsapp_app_base_url', '').rstrip('/')
                        login_url = f'{base_url}/auth/login/' if base_url else '/auth/login/'
                        plain_pw = form.cleaned_data['password']
                        notify_new_user_created(
                            whatsapp=wa,
                            name=new_user.name,
                            email=new_user.email,
                            password=plain_pw,
                            role_label=new_user.role_label,
                            login_url=login_url,
                        )
                    except Exception as _e:
                        pass  # non-fatal

                return redirect('/dashboard/users/')
    else:
        form = CreateUserForm(allowed_roles=allowed_roles, account_qs=account_qs)

    return render(request, 'admin_panel/create_user.html', {'form': form})


@login_required
@role_required(['super_admin', 'admin', 'team_head'])
def edit_user(request, user_id):
    me          = request.user
    target_user = get_object_or_404(User, id=user_id)

    # super_admin protection
    if me.role != 'super_admin' and target_user.role == 'super_admin':
        messages.error(request, 'You cannot modify super admin accounts.')
        return redirect('/dashboard/users/')

    # team_head can only edit planner/operations in their own accounts
    if me.role == 'team_head':
        if target_user.role not in ('planner', 'operations'):
            return render(request, '403.html', status=403)
        my_account_ids = set(me.accounts.values_list('id', flat=True))
        target_acct_ids = set(target_user.accounts.values_list('id', flat=True))
        if not my_account_ids & target_acct_ids:
            return render(request, '403.html', status=403)

    if me.role == 'team_head':
        account_qs = me.accounts.all()
    else:
        account_qs = Account.objects.all()

    if request.method == 'POST':
        action = request.POST.get('action')

        _ip = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
               or request.META.get('REMOTE_ADDR'))

        if action == 'toggle_active':
            target_user.is_active = not target_user.is_active
            target_user.save()
            state = 'activated' if target_user.is_active else 'deactivated'
            messages.success(request, f'{target_user.name} has been {state}.')
            AuditLog.objects.create(
                user=me, action='user_toggle',
                detail=f'{target_user.name} ({target_user.email}) {state}.',
                ip=_ip or None,
            )

        elif action == 'reset_password':
            chars   = string.ascii_letters + string.digits + '!@#$'
            new_pw  = ''.join(secrets_mod.choice(chars) for _ in range(12))
            target_user.set_password(new_pw)
            target_user.must_change_password = True
            target_user.save()
            messages.success(request,
                f'Password reset for {target_user.name}. '
                f'Temporary password: <code class="font-mono font-bold">{new_pw}</code>')
            AuditLog.objects.create(
                user=me, action='user_pwd_reset',
                detail=f'Password reset for {target_user.name} ({target_user.email}).',
                ip=_ip or None,
            )

        elif action == 'update_accounts':
            ids = request.POST.getlist('accounts')
            # team_head can only assign their own accounts
            if me.role == 'team_head':
                my_ids = set(me.accounts.values_list('id', flat=True))
                ids = [i for i in ids if int(i) in my_ids]
            target_user.accounts.set(ids)
            messages.success(request, f'Accounts updated for {target_user.name}.')
            AuditLog.objects.create(
                user=me, action='user_accounts',
                detail=f'Accounts updated for {target_user.name} ({target_user.email}).',
                ip=_ip or None,
            )

        elif action == 'update_whatsapp':
            wa = request.POST.get('whatsapp_number', '').strip()
            target_user.whatsapp_number = wa
            target_user.save(update_fields=['whatsapp_number'])
            messages.success(request, f'WhatsApp number updated for {target_user.name}.')

        return redirect('/dashboard/users/')

    return render(request, 'admin_panel/edit_user.html', {
        'target_user':   target_user,
        'all_accounts':  account_qs,
        'user_accounts': list(target_user.accounts.values_list('id', flat=True)),
    })
