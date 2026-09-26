"""Transaction guard for every agent transaction (Phase 3 c).

guard(read_only=False, rollback=False):
    transaction.atomic() + on PostgreSQL:
        SET LOCAL lock_timeout / statement_timeout / idle_in_transaction_session_timeout
        (values from AgentConfig) and, when read_only, SET TRANSACTION READ ONLY.
    A lock timeout (55P03) raises Yielded('busy_yielded'); a statement timeout (57014)
    raises Yielded('timeout_yielded'). A write attempted inside a READ ONLY block (25006)
    raises CoreWriteAttempt: that is stop-and-ask (S1), never caught and ignored.
"""
from __future__ import annotations

from contextlib import contextmanager

from django.db import DatabaseError, connection, transaction

LOCK_NOT_AVAILABLE, QUERY_CANCELED, READ_ONLY_TXN = '55P03', '57014', '25006'


class Yielded(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class CoreWriteAttempt(RuntimeError):
    """Something tried to write inside a read-only agent transaction. Stop and ask."""


def _cfg():
    from .models import AgentConfig
    return AgentConfig.objects.filter(pk=1).first() or AgentConfig()


def timeouts() -> dict:
    c = _cfg()
    return {'lock_timeout': int(c.db_lock_timeout_ms), 'statement_timeout': int(c.db_statement_timeout_ms),
            'idle_in_transaction_session_timeout': int(c.db_idle_timeout_ms)}


def apply_timeouts(statement_ms: int | None = None) -> None:
    """SET LOCAL the agent timeouts (PostgreSQL only; must be inside a transaction)."""
    if connection.vendor != 'postgresql':
        return
    t = timeouts()
    if statement_ms is not None:
        t['statement_timeout'] = int(statement_ms)
    with connection.cursor() as cur:
        for name, ms in t.items():
            cur.execute(f"SET LOCAL {name} = '{int(ms)}ms'")       # ints only: no injection


def pgcode(exc) -> str:
    return getattr(getattr(exc, '__cause__', None), 'pgcode', None) or getattr(exc, 'pgcode', '') or ''


@contextmanager
def guard(read_only: bool = False, rollback: bool = False, statement_ms: int | None = None):
    rollback = rollback or read_only        # a read-only block is always rolled back (it only reads,
    try:                                    # and a released savepoint would keep READ ONLY set)
        with transaction.atomic():
            if connection.vendor == 'postgresql':
                apply_timeouts(statement_ms)
                if read_only:
                    with connection.cursor() as cur:
                        cur.execute('SET TRANSACTION READ ONLY')
            yield
            if rollback:
                transaction.set_rollback(True)
    except DatabaseError as exc:
        code = pgcode(exc)
        if code == LOCK_NOT_AVAILABLE:
            raise Yielded('busy_yielded') from exc
        if code == QUERY_CANCELED:
            raise Yielded('timeout_yielded') from exc
        if code == READ_ONLY_TXN:
            raise CoreWriteAttempt(f'write attempted inside a read-only agent transaction: {exc}') from exc
        raise
