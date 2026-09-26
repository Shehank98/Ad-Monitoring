"""Core-table fingerprint (Phase 3 d, S6). Detection, not proof.

take():  for every table of every model of the `core` and `accounts` apps (auto-created
         many-to-many tables included): row count, max(pk) and, on PostgreSQL,
         sum(hashtext(row::text)). One scan per table, no sort.
         Each table runs in its own READ ONLY guard with its own statement_timeout
         (AgentConfig.core_fingerprint_timeout_ms); a failed table is recorded in
         'errors' and the scan continues.
diff(a, b): tables whose values differ. Anything that wrote in between (a person, a core
         thread, a core cron job) shows up, so a difference is never blamed on the agent
         by this check alone.
"""
from __future__ import annotations

import fnmatch
import time

from django.apps import apps
from django.db import DatabaseError, connection
from django.utils import timezone

from .db import CoreWriteAttempt, Yielded, guard

APPS = ('core', 'accounts')
LABEL = 'detection, not proof'
# Phase 3.1 T9: columns left out of a table's content hash (they change on every login)
HASH_EXCLUDE = {'accounts_user': ('last_login',)}
# Phase 3.1 T9: the agent-effect section lists changed tables in these two groups
RECONCILIATION_PATTERNS = ('core_schedule*', 'core_lmrbrow', 'core_tcrow', 'core_transmissionreport',
                           'core_brandmapping', 'core_manualmatch', 'core_*sponsorship*', 'core_tclmrb*',
                           'core_matchresult', 'core_summaryreportmeta', 'core_systemsetting')


def is_reconciliation_table(table: str) -> bool:
    return any(fnmatch.fnmatch(table, p) for p in RECONCILIATION_PATTERNS)


def group_tables(tables_: list) -> dict:
    """{'reconciliation': [...], 'activity': [...]} (both keys always present)."""
    rec = sorted(t for t in tables_ if is_reconciliation_table(t))
    return {'reconciliation': rec, 'activity': sorted(t for t in tables_ if t not in rec)}


def core_models() -> list:
    out = []
    for app in APPS:
        out += list(apps.get_app_config(app).get_models(include_auto_created=True))
    return sorted(out, key=lambda m: m._meta.db_table)


def tables() -> list[str]:
    return [m._meta.db_table for m in core_models()]


def _timeout_ms() -> int:
    from .models import AgentConfig
    cfg = AgentConfig.objects.filter(pk=1).first() or AgentConfig()
    return int(cfg.core_fingerprint_timeout_ms)


def take(timeout_ms: int | None = None) -> dict:
    timeout_ms = timeout_ms if timeout_ms is not None else _timeout_ms()
    qn = connection.ops.quote_name
    pg = connection.vendor == 'postgresql'
    out = {'label': LABEL, 'vendor': connection.vendor, 'taken_at': timezone.now().isoformat(),
           'tables': {}, 'errors': {}, 'timing_ms': {}}
    t_all = time.monotonic()
    for m in core_models():
        table, pk = m._meta.db_table, m._meta.pk.column
        t0 = time.monotonic()
        if pg:
            row_text = 't::text'
            drop = HASH_EXCLUDE.get(table)
            if drop:
                row_text = '(to_jsonb(t) - ' + ' - '.join(f"'{c}'" for c in drop) + ')::text'
            sql = f'SELECT count(*), max({qn(pk)}), sum(hashtext({row_text})::bigint) FROM {qn(table)} t'
        else:
            sql = f'SELECT count(*), max({qn(pk)}) FROM {qn(table)}'
        try:
            with guard(read_only=True, statement_ms=timeout_ms):
                with connection.cursor() as cur:
                    cur.execute(sql)
                    row = cur.fetchone()
        except CoreWriteAttempt:
            raise
        except (Yielded, DatabaseError) as exc:
            out['errors'][table] = f'{type(exc).__name__}: {exc}'[:300]
            out['timing_ms'][table] = round((time.monotonic() - t0) * 1000, 1)
            continue
        out['tables'][table] = {'count': int(row[0]), 'max_id': None if row[1] is None else str(row[1]),
                                'hash': None if not pg or row[2] is None else str(row[2])}
        out['timing_ms'][table] = round((time.monotonic() - t0) * 1000, 1)
    out['total_ms'] = round((time.monotonic() - t_all) * 1000, 1)
    return out


def diff(a: dict | None, b: dict | None) -> dict:
    """{table: {'start': {...}, 'end': {...}}} for every table whose values differ."""
    ta, tb = (a or {}).get('tables', {}), (b or {}).get('tables', {})
    return {t: {'start': ta.get(t), 'end': tb.get(t)}
            for t in sorted(set(ta) | set(tb)) if ta.get(t) != tb.get(t)}
