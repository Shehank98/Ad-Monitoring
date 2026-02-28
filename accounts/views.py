import secrets as secrets_mod
import string

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from core.models import Account
from .decorators import role_required
from .forms import ChangePasswordForm, CreateUserForm, LoginForm
from .models import User


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
                return render(request, 'accounts/login.html', {'form': form})

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
                return redirect('/dashboard/')
    else:
        form = LoginForm()

    return render(request, 'accounts/login.html', {'form': form})


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


# ── User management (admin / super_admin only) ────────────────────────────────

@login_required
@role_required(['super_admin', 'admin'])
def user_list(request):
    qs = User.objects.select_related('created_by').prefetch_related('accounts')
    if request.user.role != 'super_admin':
        qs = qs.exclude(role='super_admin')
    return render(request, 'admin_panel/users.html', {'users': qs})


@login_required
@role_required(['super_admin', 'admin'])
def create_user(request):
    me            = request.user
    allowed_roles = me.creatable_roles()

    if request.method == 'POST':
        form = CreateUserForm(request.POST, allowed_roles=allowed_roles)
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
                    created_by=me,
                    must_change_password=True,
                )
                if form.cleaned_data.get('accounts'):
                    new_user.accounts.set(form.cleaned_data['accounts'])
                messages.success(request, f'User {new_user.name} created successfully.')
                return redirect('/dashboard/users/')
    else:
        form = CreateUserForm(allowed_roles=allowed_roles)

    return render(request, 'admin_panel/create_user.html', {'form': form})


@login_required
@role_required(['super_admin', 'admin'])
def edit_user(request, user_id):
    me          = request.user
    target_user = get_object_or_404(User, id=user_id)

    if me.role != 'super_admin' and target_user.role == 'super_admin':
        messages.error(request, 'You cannot modify super admin accounts.')
        return redirect('/dashboard/users/')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'toggle_active':
            target_user.is_active = not target_user.is_active
            target_user.save()
            state = 'activated' if target_user.is_active else 'deactivated'
            messages.success(request, f'{target_user.name} has been {state}.')

        elif action == 'reset_password':
            chars   = string.ascii_letters + string.digits + '!@#$'
            new_pw  = ''.join(secrets_mod.choice(chars) for _ in range(12))
            target_user.set_password(new_pw)
            target_user.must_change_password = True
            target_user.save()
            messages.success(request,
                f'Password reset for {target_user.name}. '
                f'Temporary password: <code class="font-mono font-bold">{new_pw}</code>')

        elif action == 'update_accounts':
            ids = request.POST.getlist('accounts')
            target_user.accounts.set(ids)
            messages.success(request, f'Accounts updated for {target_user.name}.')

        return redirect('/dashboard/users/')

    return render(request, 'admin_panel/edit_user.html', {
        'target_user':   target_user,
        'all_accounts':  Account.objects.all(),
        'user_accounts': list(target_user.accounts.values_list('id', flat=True)),
    })
