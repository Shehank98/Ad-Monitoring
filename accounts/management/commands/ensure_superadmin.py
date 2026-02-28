"""
Management command: ensure_superadmin
--------------------------------------
Reads SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD from environment variables
and creates the first Super Admin user if none exists yet.

Called automatically from Procfile on every startup — safe to run repeatedly
because it does nothing if a super_admin account already exists.
"""
import os
from django.core.management.base import BaseCommand
from accounts.models import User


class Command(BaseCommand):
    help = 'Create the initial Super Admin user from environment variables if none exists.'

    def handle(self, *args, **options):
        if User.objects.filter(role='super_admin').exists():
            self.stdout.write('Super admin already exists — skipping.')
            return

        email    = os.environ.get('SUPER_ADMIN_EMAIL', '').strip()
        password = os.environ.get('SUPER_ADMIN_PASSWORD', '').strip()
        name     = os.environ.get('SUPER_ADMIN_NAME', 'Super Admin').strip()

        if not email or not password:
            self.stderr.write(
                'SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD must be set to '
                'create the first admin. Skipping.'
            )
            return

        user = User.objects.create_superuser(
            email=email,
            name=name,
            password=password,
        )
        user.must_change_password = False
        user.save()
        self.stdout.write(
            self.style.SUCCESS(f'Super admin created: {email}')
        )
