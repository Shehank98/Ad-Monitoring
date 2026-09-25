"""Intake-wide lock (owner C9): overlapping cron runs exit at once.

PostgreSQL: a session-level pg_try_advisory_lock (the run spans many transactions and
LLM calls), released in finally. Other databases: an agent ScopeLockRow with a fixed key.
"""
from contextlib import contextmanager

from django.db import connection

from agent.locks import ScopeBusy, _acquire_row, _release_row, is_postgres

INTAKE_LOCK_KEY = 0x1A7A_4E00_0000_0001   # fixed, outside the scope-key hash space in practice


@contextmanager
def intake_lock(ttl_minutes: int = 30):
    """Yields True when this run holds the lock, False when another run holds it."""
    if is_postgres():
        with connection.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s)', [INTAKE_LOCK_KEY])
            (ok,) = cur.fetchone()
        try:
            yield bool(ok)
        finally:
            if ok:
                with connection.cursor() as cur:
                    cur.execute('SELECT pg_advisory_unlock(%s)', [INTAKE_LOCK_KEY])
        return
    try:
        owner = _acquire_row(INTAKE_LOCK_KEY, ttl_minutes)
    except ScopeBusy:
        yield False
        return
    try:
        yield True
    finally:
        _release_row(INTAKE_LOCK_KEY, owner)
