"""Phase 3 d + S8: a full agent_cycle, shadow dry runs included, writes nothing to core or accounts.

- row count and content hash (every concrete field, sorted by pk) of every table of every
  model of the `core` and `accounts` apps, before and after
- a spy on gate.perform over the same cycles: zero calls targeting core.* or accounts.*
- PostgreSQL: the sequences that advance during the rolled-back dry runs are listed (nextval
  is not transactional, so id gaps are expected)
"""
import datetime
import hashlib
import json
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from agent import core_fingerprint, cycle, gate
from agent.models import AgentConfig, AgentRun, SummarySnapshot
from agent.service import ensure_service_user
from core.models import Schedule, SummaryReportMeta

from . import factories as f

TZ = timezone.get_current_timezone()
DAY = timezone.make_aware(datetime.datetime(2025, 2, 10, 12), TZ)
NIGHT = timezone.make_aware(datetime.datetime(2025, 2, 10, 2), TZ)
AFTER = timezone.make_aware(datetime.datetime(2025, 2, 10, 7, 45), TZ)      # window closed, digest due


def core_state() -> dict:
    out = {}
    for m in core_fingerprint.core_models():
        fields = [fl.attname for fl in m._meta.concrete_fields]
        rows = list(m._default_manager.order_by('pk').values_list(*fields))
        blob = json.dumps(rows, default=str, sort_keys=True).encode()
        out[m._meta.db_table] = (len(rows), hashlib.sha256(blob).hexdigest())
    return out


def sequences() -> dict:
    if connection.vendor != 'postgresql':
        return {}
    with connection.cursor() as cur:
        cur.execute('SELECT sequencename, last_value FROM pg_sequences ORDER BY 1')
        return dict(cur.fetchall())


@override_settings(AGENT_ALLOW_SQLITE_DRY_RUN=True)
class NoCoreWritesTest(TransactionTestCase):
    def setUp(self):
        ensure_service_user()
        AgentConfig.objects.update_or_create(pk=1, defaults={'enabled': True})
        self.admin = f.user(role='admin', email='boss@x.lk')
        acc, s = f.full_scope()
        f.full_scope(acc, number='201', brand='Other', channel='Derana TV')
        acc2, s2 = f.full_scope(f.account('Cargills'), number='301', channel='Hiru TV')
        SummaryReportMeta.objects.create(account=acc2, channel=s2.channel, month=s2.month, authorised_by='FH')
        old = timezone.now() - datetime.timedelta(days=2)
        Schedule.objects.update(uploaded_at=old)
        from core.models import MonitoringData, TransmissionReport
        MonitoringData.objects.update(uploaded_at=old)
        TransmissionReport.objects.update(uploaded_at=old)

    def test_full_cycle_with_shadow_runs_writes_no_core_row(self):
        before, seq_before = core_state(), sequences()
        with mock.patch.object(gate, 'perform', wraps=gate.perform) as spy:
            day = cycle.run_cycle(now=DAY)
            night = cycle.run_cycle(now=NIGHT)
            cycle.run_cycle(now=AFTER)
        after, seq_after = core_state(), sequences()

        # the cycles really did the work (not a vacuous pass)
        self.assertEqual(day['counts']['observed'], 3)
        self.assertEqual(night['counts']['shadow_ok'], 2)            # the authorised scope is skipped (S5)
        self.assertEqual(AgentRun.objects.filter(kind='dry_run').count(), 2)
        self.assertEqual(SummarySnapshot.objects.filter(kind='shadow').count(), 2)
        self.assertTrue(AgentRun.objects.filter(kind='shadow_window', status='ok').exists())
        self.assertTrue(AgentRun.objects.filter(kind='digest').exists())

        changed = sorted(t for t in before if before[t] != after[t])
        self.assertEqual(changed, [], f'core/accounts tables changed: {changed}')

        # S8: no gate.perform call targets core.* or accounts.* (at level 0 there are none at all)
        targets = [c.kwargs.get('target_model', '') for c in spy.call_args_list]
        self.assertEqual([t for t in targets if t.startswith(('core.', 'accounts.'))], [])
        self.assertEqual(targets, [])

        moved = sorted(k for k in seq_after if seq_after[k] != seq_before.get(k))
        print(f'\n[no_core_writes] vendor={connection.vendor} sequences advanced by rolled-back dry runs: '
              f'{[m for m in moved if m.startswith(("core_", "accounts_"))]}')
        # agent sequences move because agent tables are written; core ones only by rolled-back inserts
        self.assertTrue(all(before[t][0] == after[t][0] for t in before))
