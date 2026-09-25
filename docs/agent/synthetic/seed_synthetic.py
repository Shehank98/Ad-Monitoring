"""SYNTHETIC DATA seed for the Phase 1 audit and golden-check dry runs.

Run on an EMPTY, disposable database only:
    DATABASE_URL=postgres://…/scratch python manage.py migrate
    DATABASE_URL=postgres://…/scratch python manage.py shell < docs/agent/synthetic/seed_synthetic.py

Builds four clients. Each existing-system issue from Phase 0 appears at least once, so
agent_core_audit has something to count. Scopes are then reconciled the way a person
does today (engines in smart mode, per schedule).
"""
from agent.tests import factories as f
from core.models import BrandMapping, LMRBRow, ManualMatch, ScheduleRow, TransmissionReport
from verification.engine import run_scope
from verification.sponsorship_engine import reconcile_sponsorship
from verification.tc_engine import reconcile_tc

assert not ScheduleRow.objects.exists(), 'seed only an empty, disposable database'


def reconcile(acc, schedules):
    run_scope(acc.id, f.CHANNEL, f.MONTH, 'smart')
    for s in schedules:
        reconcile_tc(acc.id, f.CHANNEL, f.MONTH, mode='smart', schedule_id=s.id)
        reconcile_sponsorship(acc.id, f.CHANNEL, f.MONTH, mode='smart', schedule_id=s.id)


# 1. Keells — revised schedule (v1 superseded by v2), clean mapping
keells = f.account('Keells (synthetic)')
v1 = f.schedule(keells, number='101', version=1, superseded=True)
for d in (9, 10):
    f.row(keells, v1, day=d)
v2 = f.schedule(keells, number='101', version=2)
for d in (10, 12, 14):
    f.row(keells, v2, day=d)
    f.lmrb(keells, day=d)
f.lmrb(keells, day=31, time='23:00:00', theme='Other')
f.mapping(keells)
rep = f.tc_report(keells, v2, rows=3)
for d in (10, 12, 14):
    f.tc_row(keells, rep, day=d)
reconcile(keells, [v2])

# 2. Dialog — several schedules in one scope; one brand mapped only by a wildcard tc_theme
dialog = f.account('Dialog (synthetic)')
s1 = f.schedule(dialog, number='201')
s2 = f.schedule(dialog, number='202')
f.mapping(dialog, brand='Fibre', theme='Fibre (30)', tc='FIBRE 30')
f.mapping(dialog, brand='HBB', theme='HBB (30)', tc='HBB*')
for s, d, t in ((s1, 10, '20:05:00'), (s2, 11, '20:06:00')):
    f.row(dialog, s, brand='Fibre', day=d)
    f.lmrb(dialog, day=d, time=t, theme='Fibre (30)')
    r = f.tc_report(dialog, s, rows=1)
    f.tc_row(dialog, r, day=d, time=t, theme='FIBRE 30')
f.row(dialog, s1, brand='HBB', day=15)
f.lmrb(dialog, day=15, time='20:04:00', theme='HBB (30)')
f.tc_row(dialog, TransmissionReport.objects.get(schedule=s1), day=15, time='20:04:01', theme='HBB 30 NEW')
f.lmrb(dialog, day=31, time='23:00:00', theme='Other')
reconcile(dialog, [s1, s2])

# 3. Elephant House — keep_both duplicate, locked schedule, unlinked TC, channel variant
eh = f.account('Elephant House (synthetic)')
d1 = f.schedule(eh, number='301', version=1)
d2 = f.schedule(eh, number='301', version=2)              # keep_both: neither superseded
f.row(eh, d2, brand='Soda', day=10)
f.schedule(eh, number='99', locked=True)                  # mixed width + locked
f.tc_report(eh, None)                                      # TC not linked
f.tc_report(eh, None, channel='SIRASA TV')                 # case variant

# 4. Cargills — lock inconsistencies (orphaned flags, a manual match that lost its lock)
cg = f.account('Cargills (synthetic)')
c1 = f.schedule(cg, number='401')
sr = f.row(cg, c1, brand='Kotmale', day=10)
lr = f.lmrb(cg, day=10, theme='Kotmale (30)')
ManualMatch.objects.create(account=cg, channel=f.CHANNEL, month=f.MONTH, schedule_row=sr, lmrb_row=lr)
f.lmrb(cg, day=11, time='10:00:00', theme='Kotmale (30)', is_tc_lmrb_matched=True)
f.lmrb(cg, day=11, time='11:00:00', theme='Kotmale (30)', is_sponsorship_matched=True)
f.lmrb(cg, day=11, time='12:00:00', theme='Kotmale (30)', is_matched=True, is_sponsorship_matched=True)
print('SYNTHETIC seed complete:', ScheduleRow.objects.count(), 'schedule rows,',
      LMRBRow.objects.count(), 'LMRB rows,', BrandMapping.objects.count(), 'mappings')
