"""Agent service user (Amendment A9, owner decision 6): role operations, unusable password,
every Account assigned. It is the actor on every AgentAction.

Two entry points with different rules (guardian check 18):
  ensure_service_user()  run by a PERSON (manage.py agent_ensure_service_user). May create
                         the user and set role/password. No AgentAction; the command prints
                         everything created or changed.
  sync_service_user()    run by the SYSTEM (agent_cycle). Asserts role == 'operations'; on
                         failure it logs an error, sets a Heartbeat alert and raises, which
                         stops the cycle. It may only ADD accounts, and records an AgentAction
                         (service_user_account_sync) with before/after account id lists. It
                         never removes accounts, never changes role, password or is_active,
                         and never touches another user.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from core.models import Account

log = logging.getLogger('agent')

DEFAULT_EMAIL = 'reconciliation-agent@agent.invalid'
HEARTBEAT = 'agent_cycle'


class ServiceUserError(RuntimeError):
    """The service user is missing or its role is not 'operations': the cycle must stop."""


def service_email() -> str:
    return getattr(settings, 'AGENT_SERVICE_USER_EMAIL', DEFAULT_EMAIL)


def get_service_user():
    return User.objects.filter(email=service_email()).first()


def _account_ids(user) -> list:
    return sorted(user.accounts.order_by().values_list('id', flat=True))


def ensure_service_user():
    """Person-run: create or refresh the service user.
    Returns (user, created, changes) where changes lists every field/account change made."""
    email = service_email()
    user = User.objects.filter(email=email).first()
    created = user is None
    changes = []
    if created:
        user = User.objects.create_user(email=email, name='Reconciliation Agent', password=None,
                                        role='operations', must_change_password=False)
        changes.append(f'created user {email} (id {user.id}, role operations, unusable password)')
    changed = []
    if user.role != 'operations':
        changes.append(f'role: {user.role} -> operations')
        user.role = 'operations'
        changed.append('role')
    if user.has_usable_password():
        user.set_unusable_password()
        changed.append('password')
        changes.append('password: usable -> unusable')
    if changed:
        user.save(update_fields=changed)
    have = set(_account_ids(user))
    missing = sorted(Account.objects.exclude(id__in=have).values_list('id', flat=True))
    if missing:
        user.accounts.add(*missing)
        changes.append(f'accounts added: {missing}')
    return user, created, changes


def _alert(message: str) -> None:
    from .models import Heartbeat
    log.error('agent service user check failed: %s', message)
    Heartbeat.objects.update_or_create(
        name=HEARTBEAT, defaults={'last_beat': timezone.now(), 'alert': True, 'alert_message': message})


def require_service_user():
    """Assert the service user exists with role 'operations' (checked on every system run).
    On failure: log an error, set the Heartbeat alert, raise ServiceUserError (stop)."""
    user = get_service_user()
    if user is None:
        msg = f'service user {service_email()} does not exist; run manage.py agent_ensure_service_user'
        _alert(msg)
        raise ServiceUserError(msg)
    if user.role != 'operations':
        msg = f'service user {user.email} has role {user.role!r}, expected operations; cycle stopped'
        _alert(msg)
        raise ServiceUserError(msg)
    return user


def sync_service_user(run=None):
    """System-run (agent_cycle). Returns (user, AgentAction | None). Raises ServiceUserError."""
    from . import gate
    user = require_service_user()
    before = _account_ids(user)
    missing = sorted(Account.objects.exclude(id__in=before).values_list('id', flat=True))
    if not missing:
        return user, None

    def apply():
        user.accounts.add(*missing)          # add only: never remove, never touch other fields
        return {'account_ids': _account_ids(user)}
    with transaction.atomic():
        action = gate.perform(tier=gate.T0, action_type='service_user_account_sync',
                              target_model='accounts.User', target_pk=user.id,
                              before={'account_ids': before}, apply=apply,
                              reason=f'service user re-sync: added accounts {missing}',
                              evidence={'added': missing}, actor=user, run=run)
    return user, action
