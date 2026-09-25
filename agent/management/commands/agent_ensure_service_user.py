from django.core.management.base import BaseCommand

from agent.service import ensure_service_user


class Command(BaseCommand):
    help = 'Create or refresh the Reconciliation Agent service user (role operations, all accounts).'

    def handle(self, *args, **opts):
        user, created, added = ensure_service_user()
        self.stdout.write(f"{'Created' if created else 'Checked'} {user.email} "
                          f"(role={user.role}, usable_password={user.has_usable_password()}); "
                          f"accounts added: {added}; total: {user.accounts.count()}")
