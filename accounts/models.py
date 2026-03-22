from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, name, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user  = self.model(email=email, name=name, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, name, password=None, **extra_fields):
        extra_fields.setdefault('role', 'super_admin')
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('must_change_password', False)
        return self.create_user(email, name, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    ROLES = [
        ('super_admin',     'Super Admin'),
        ('admin',           'Admin'),
        ('team_head',       'Team Head'),
        ('planner',         'Planner'),
        ('operations',      'Operations'),
        ('channel_officer', 'Channel Officer'),
    ]

    ROLE_COLORS = {
        'super_admin':     'purple',
        'admin':           'blue',
        'team_head':       'green',
        'planner':         'amber',
        'operations':      'orange',
        'channel_officer': 'teal',
    }

    email                = models.EmailField(unique=True)
    name                 = models.CharField(max_length=150)
    role                 = models.CharField(max_length=20, choices=ROLES, default='planner')
    whatsapp_number      = models.CharField(max_length=20, blank=True, default='',
                                            help_text='Include country code, e.g. +94771234567')
    # accounts assigned to this user (relevant for team_head, planner)
    accounts             = models.ManyToManyField('core.Account', blank=True, related_name='users')
    created_by           = models.ForeignKey('self', null=True, blank=True,
                                             on_delete=models.SET_NULL, related_name='created_users')
    is_active            = models.BooleanField(default=True)
    is_staff             = models.BooleanField(default=False)
    date_joined          = models.DateTimeField(auto_now_add=True)
    must_change_password = models.BooleanField(default=True)

    objects = UserManager()

    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = ['name']

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} <{self.email}>'

    # ── Convenience properties ─────────────────────────────────────────────

    @property
    def role_label(self):
        return dict(self.ROLES).get(self.role, self.role)

    @property
    def role_color(self):
        return self.ROLE_COLORS.get(self.role, 'slate')

    @property
    def initials(self):
        parts = self.name.split()
        return ''.join(p[0].upper() for p in parts[:2]) if parts else '?'

    # Hierarchical helpers
    ROLE_HIERARCHY = {'super_admin': 5, 'admin': 4, 'team_head': 3, 'planner': 2, 'operations': 2, 'channel_officer': 1}

    CAN_CREATE = {
        'super_admin': ['admin', 'team_head', 'planner', 'operations', 'channel_officer'],
        'admin':       ['team_head', 'planner', 'operations', 'channel_officer'],
        'team_head':   ['planner', 'operations'],
    }

    def can_create_role(self, role: str) -> bool:
        return role in self.CAN_CREATE.get(self.role, [])

    def creatable_roles(self) -> list:
        return self.CAN_CREATE.get(self.role, [])
