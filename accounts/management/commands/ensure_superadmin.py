import os
from django.core.management.base import BaseCommand
from accounts.models import User


class Command(BaseCommand):
    help = 'Create or update the Super Admin user from environment variables.'

    def handle(self, *args, **options):
        email    = os.environ.get('SUPER_ADMIN_EMAIL', '').strip()
        password = os.environ.get('SUPER_ADMIN_PASSWORD', '').strip()
        name     = os.environ.get('SUPER_ADMIN_NAME', 'Super Admin').strip()

        if not email or not password:
            self.stderr.write(
                'SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD must be set. Skipping.'
            )
            return

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'name': name,
                'role': 'super_admin',
                'is_staff': True,
                'is_superuser': True,
                'must_change_password': False,
            }
        )
        # Always sync the password from the env var so changing it takes effect
        user.set_password(password)
        if not created:
            user.role             = 'super_admin'
            user.is_staff         = True
            user.is_superuser     = True
            user.must_change_password = False
        user.save()

        verb = 'created' if created else 'updated'
        self.stdout.write(self.style.SUCCESS(f'Super admin {verb}: {email}'))
