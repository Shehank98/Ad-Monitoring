"""Role and account scoping for agent pages (Amendment A9). Reuses core helpers."""
from core.views import _account_qs, _is_admin

ADMIN_ROLES = ('super_admin', 'admin')
USER_ROLES = ('team_head', 'planner', 'operations')
AGENT_ROLES = ADMIN_ROLES + USER_ROLES          # channel_officer: no access


def is_agent_admin(user) -> bool:
    return bool(user and user.is_authenticated and _is_admin(user))


def can_view_agent(user) -> bool:
    return bool(user and user.is_authenticated and user.role in AGENT_ROLES)


def account_ids_for(user) -> list[int]:
    return list(_account_qs(user).values_list('id', flat=True))
