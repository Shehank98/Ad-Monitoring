"""
Action Gate (brief §8 as amended). Every agent write to a core table goes through
perform(); it re-reads AgentConfig immediately before the write (kill switch), checks
the tier, and records an AgentAction with before and after values.

Tiers: T0 read (always) · T1 safe (level >= 1) · T2 aliases (never auto, A8) ·
T3 exact mappings (level 3 + every condition) · T4 human only (never auto).
Wildcard values are never auto-applied (guardian check 6).
"""
from __future__ import annotations

from .models import AgentAccountOverride, AgentAction, AgentConfig

T0, T1, T2, T3, T4 = 0, 1, 2, 3, 4
T3_CONDITIONS = ('confidence_ok', 'only_target_changed', 'no_authorised_change', 'no_conflict', 'exact_value')


class AgentDisabled(Exception):
    pass


class TierNotAllowed(Exception):
    pass


def _fresh_config() -> AgentConfig:
    return AgentConfig.objects.filter(pk=1).first() or AgentConfig()   # unsaved default: disabled


def is_enabled(account_id=None) -> bool:
    cfg = _fresh_config()
    if not cfg.enabled:
        return False
    if account_id is not None:
        ov = AgentAccountOverride.objects.filter(account_id=account_id).first()
        if ov and ov.enabled is False:
            return False
    return True


def effective_level(account_id=None) -> int:
    level = _fresh_config().autonomy_level
    if account_id is not None:
        ov = AgentAccountOverride.objects.filter(account_id=account_id).first()
        if ov and ov.autonomy_level is not None:
            level = min(level, ov.autonomy_level)
    return level


def ensure_enabled(account_id=None) -> None:
    if not is_enabled(account_id):
        raise AgentDisabled('Reconciliation Agent is disabled (AgentConfig.enabled=False)')


def has_wildcard(values) -> bool:
    """True if any value (or any pipe-separated part of one) contains '*'. The gate checks
    this itself; it never trusts a caller's exact_value flag alone (guardian note 6)."""
    for v in values or ():
        if v is not None and '*' in str(v):
            return True
    return False


def allowed(tier: int, account_id=None, conditions: dict | None = None, values=None) -> bool:
    """`values`: for T3, every value the write would store (e.g. theme, tc_theme). T3 is
    refused when it is missing or any value carries a wildcard."""
    if tier == T0:
        return True
    if not is_enabled(account_id):
        return False
    level = effective_level(account_id)
    if tier == T1:
        return level >= 1
    if tier == T2:
        return False                      # A8: aliases are proposal-only
    if tier == T3:
        c = conditions or {}
        if not values or has_wildcard(values):
            return False
        return level >= 3 and all(c.get(k) is True for k in T3_CONDITIONS)
    return False                          # T4 and anything else: human only


def perform(*, tier: int, action_type: str, scope=None, target_model: str = '', target_pk='',
            before: dict, apply, reason: str = '', evidence: dict | None = None,
            actor=None, run=None, conditions: dict | None = None, account_id=None,
            values=None) -> AgentAction:
    """Run `apply()` (which returns the `after` dict) only if the gate allows it.
    The kill switch is read again immediately before the write."""
    account_id = account_id if account_id is not None else getattr(scope, 'account_id', None)
    if tier != T0:
        ensure_enabled(account_id)
        if not allowed(tier, account_id, conditions, values):
            raise TierNotAllowed(f'tier T{tier} is not allowed at level {effective_level(account_id)}')
        ensure_enabled(account_id)        # immediately before the write
    after = apply()
    return AgentAction.objects.create(
        actor=actor, tier=tier, action_type=action_type, scope=scope, target_model=target_model,
        target_pk=str(target_pk), before=before, after=after or {}, reason=reason,
        evidence=evidence or {}, run=run)
