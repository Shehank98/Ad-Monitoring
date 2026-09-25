from django.core.management.base import BaseCommand

from agent.service import ensure_service_user


class Command(BaseCommand):
    help = 'Create or refresh the Reconciliation Agent service user (role operations, all accounts).'

    def handle(self, *args, **opts):
        # Run by a person: no AgentAction (owner decision 6). Print every change made.
        user, created, changes = ensure_service_user()
        for c in changes:
            self.stdout.write(f'  changed: {c}')
        self.stdout.write(f"{'Created' if created else 'Checked'} {user.email} "
                          f"(role={user.role}, usable_password={user.has_usable_password()}, "
                          f"is_active={user.is_active}); changes: {len(changes)}; "
                          f"accounts: {user.accounts.count()}")
