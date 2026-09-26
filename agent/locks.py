"""
ScopeLock (Amendment P2).

PostgreSQL: pg_try_advisory_xact_lock(key), taken INSIDE the caller's
transaction.atomic() block, so it is released on commit, rollback or crash.

Other databases: a ScopeLockRow with expires_at (default 15 minutes). The row is
committed before the work starts so other workers see it, expired rows are treated
as free, and the row is always released in a finally block.
"""
from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .models import ScopeLockRow

DEFAULT_TTL_MINUTES = 15


class ScopeBusy(Exception):
    """Another worker holds the lock for this scope."""


def _owner() -> str:
    return f'{socket.gethostname()}:{os.getpid()}'


def is_postgres() -> bool:
    return connection.vendor == 'postgresql'


def take_xact_lock(key: int) -> None:
    """PostgreSQL only. Must be called inside transaction.atomic()."""
    if not connection.in_atomic_block:
        raise RuntimeError('pg_try_advisory_xact_lock must be taken inside transaction.atomic()')
    with connection.cursor() as cur:
        cur.execute('SELECT pg_try_advisory_xact_lock(%s)', [key])
        (ok,) = cur.fetchone()
    if not ok:
        raise ScopeBusy(f'scope lock {key} is held by another transaction')


def _acquire_row(key: int, ttl_minutes: int) -> str:
    now = timezone.now()
    owner = _owner()
    with transaction.atomic():
        row = ScopeLockRow.objects.select_for_update().filter(key=key).first()
        if row and row.expires_at > now:
            raise ScopeBusy(f'scope lock {key} is held by {row.owner} until {row.expires_at:%H:%M:%S}')
        if row:   # expired: treat as free and take it over
            row.owner, row.acquired_at, row.expires_at = owner, now, now + timedelta(minutes=ttl_minutes)
            row.save(update_fields=['owner', 'acquired_at', 'expires_at'])
        else:
            try:
                ScopeLockRow.objects.create(key=key, owner=owner, acquired_at=now,
                                            expires_at=now + timedelta(minutes=ttl_minutes))
            except IntegrityError as exc:
                raise ScopeBusy(f'scope lock {key} was taken concurrently') from exc
    return owner


def _release_row(key: int, owner: str) -> None:
    ScopeLockRow.objects.filter(key=key, owner=owner).delete()


@contextmanager
def fallback_lock(key: int, ttl_minutes: int = DEFAULT_TTL_MINUTES):
    """Non-PostgreSQL lock. Use OUTSIDE the work's transaction.atomic()."""
    if is_postgres():
        yield
        return
    if connection.in_atomic_block:
        raise RuntimeError('fallback_lock must be acquired outside transaction.atomic()')
    owner = _acquire_row(key, ttl_minutes)
    try:
        yield
    finally:
        _release_row(key, owner)


@contextmanager
def scope_lock(key: int, ttl_minutes: int = DEFAULT_TTL_MINUTES):
    """fallback_lock + transaction.atomic() + (PostgreSQL) xact advisory lock.

    Usage:
        with scope_lock(key):
            ...engine calls...   # inside one atomic block, lock held
    """
    with fallback_lock(key, ttl_minutes):
        with transaction.atomic():
            if is_postgres():
                from .db import apply_timeouts          # Phase 3: every agent transaction
                apply_timeouts()
                take_xact_lock(key)
            yield
