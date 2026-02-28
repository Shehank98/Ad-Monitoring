from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display  = ('email', 'name', 'role', 'is_active', 'date_joined')
    list_filter   = ('role', 'is_active')
    search_fields = ('email', 'name')
    ordering      = ('name',)
    filter_horizontal = ('accounts', 'groups', 'user_permissions')

    fieldsets = (
        (None,           {'fields': ('email', 'password')}),
        ('Personal',     {'fields': ('name',)}),
        ('Role & Access', {'fields': ('role', 'accounts', 'must_change_password')}),
        ('Permissions',  {'fields': ('is_active', 'is_staff', 'is_superuser',
                                     'groups', 'user_permissions')}),
        ('Meta',         {'fields': ('created_by',)}),
    )
    add_fieldsets = (
        (None, {'classes': ('wide',), 'fields': ('email', 'name', 'role', 'password1', 'password2')}),
    )
