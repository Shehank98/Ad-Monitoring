"""Agent service user (Amendment A9): role operations, unusable password, every
Account assigned; re-synced on every agent cycle. It is the actor on every AgentAction."""
from django.conf import settings

from accounts.models import User
from core.models import Account

DEFAULT_EMAIL = 'reconciliation-agent@agent.invalid'


def service_email() -> str:
    return getattr(settings, 'AGENT_SERVICE_USER_EMAIL', DEFAULT_EMAIL)


def ensure_service_user():
    """Create or refresh the service user. Returns (user, created, accounts_added)."""
    email = service_email()
    user = User.objects.filter(email=email).first()
    created = user is None
    if created:
        user = User.objects.create_user(email=email, name='Reconciliation Agent', password=None,
                                        role='operations', must_change_password=False)
    changed = []
    if user.role != 'operations':
        user.role = 'operations'
        changed.append('role')
    if user.has_usable_password():
        user.set_unusable_password()
        changed.append('password')
    if changed:
        user.save(update_fields=changed)
    have = set(user.accounts.values_list('id', flat=True))
    missing = list(Account.objects.exclude(id__in=have).values_list('id', flat=True))
    if missing:
        user.accounts.add(*missing)
    return user, created, len(missing)


def get_service_user():
    return User.objects.filter(email=service_email()).first()
