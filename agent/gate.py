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


ACTOR_KINDS = ('agent', 'intake_runner', 'intake_fetch', 'intake_retention', 'human')
HUMAN_ROLES = ('super_admin', 'admin')


class IntakeModeOff(Exception):
    """The intake runner may only write in tc_intake_mode='suggest'."""


class FetchDisabled(Exception):
    """Mail fetch needs AgentConfig.intake_fetch_enabled and an active AllowedSender."""


class HumanNotAllowed(Exception):
    """A human write needs an admin actor and every per-action condition True."""


def _check_actor(actor_kind, tier, account_id, conditions, values, actor):
    """The per-actor rules (owner Q6). One write path; the rules differ by who writes.

    agent          kill switch (re-read) + tier rules
    intake_runner  kill switch (re-read) + tc_intake_mode == 'suggest'
    intake_fetch   NO kill switch; intake_fetch_enabled + >=1 active AllowedSender
    intake_retention NO kill switch, no conditions: only clears stored bytes/bodies of
                   finished intake items (C6), so retention keeps running even after
                   fetch is switched off
    human          NO kill switch; actor role super_admin/admin + every condition True
    """
    if actor_kind == 'agent':
        ensure_enabled(account_id)
        if tier != T0 and not allowed(tier, account_id, conditions, values):
            raise TierNotAllowed(f'tier T{tier} is not allowed at level {effective_level(account_id)}')
    elif actor_kind == 'intake_runner':
        ensure_enabled(account_id)
        if _fresh_config().tc_intake_mode != 'suggest':
            raise IntakeModeOff('the intake runner writes only in suggest mode')
    elif actor_kind == 'intake_fetch':
        from intake.models import AllowedSender
        if not _fresh_config().intake_fetch_enabled:
            raise FetchDisabled('mail fetch is off (AgentConfig.intake_fetch_enabled=False)')
        if not AllowedSender.objects.filter(active=True).exists():
            raise FetchDisabled('no active AllowedSender')
    elif actor_kind == 'intake_retention':
        pass
    elif actor_kind == 'human':
        if actor is None or getattr(actor, 'role', None) not in HUMAN_ROLES:
            raise HumanNotAllowed('only super_admin or admin may make this change')
        bad = [k for k, v in (conditions or {}).items() if v is not True]
        if bad:
            raise HumanNotAllowed(f'conditions not met: {", ".join(sorted(bad))}')
    else:
        raise ValueError(f'unknown actor_kind {actor_kind!r}')


def perform(*, tier: int, action_type: str, scope=None, target_model: str = '', target_pk='',
            before: dict, apply, reason: str = '', evidence: dict | None = None,
            actor=None, run=None, conditions: dict | None = None, account_id=None,
            values=None, actor_kind: str = 'agent') -> AgentAction:
    """The one write path. Run `apply()` (which returns the `after` dict) only if the
    rules for `actor_kind` allow it, then record an AgentAction with before and after.

    The rules are checked twice: before anything else, and again immediately before
    apply(), so a kill switch (or a mode/fetch switch) flipped in between still stops
    the write. perform() is only used for writes, so even a T0 agent write stops when
    the agent is disabled (guardian check 5). Human (admin) writes are not blocked by the
    kill switch; they are logged with human_confirmed=True (owner Q6)."""
    account_id = account_id if account_id is not None else getattr(scope, 'account_id', None)
    _check_actor(actor_kind, tier, account_id, conditions, values, actor)
    _check_actor(actor_kind, tier, account_id, conditions, values, actor)   # immediately before
    after = apply()
    return AgentAction.objects.create(
        actor=actor, tier=tier, action_type=action_type, scope=scope, target_model=target_model,
        target_pk=str(target_pk), before=before, after=after or {}, reason=reason,
        evidence=evidence or {}, run=run, actor_kind=actor_kind,
        human_confirmed=actor_kind == 'human')
