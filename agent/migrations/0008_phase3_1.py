"""Phase 3.1: BASELINE is a state, not a finding. Remove it from the findings ledger
(agent table only; BASELINE never had proposals)."""
from django.db import migrations


def drop_baseline(apps, schema_editor):
    apps.get_model('agent', 'FindingLedger').objects.filter(code='BASELINE').delete()


class Migration(migrations.Migration):
    dependencies = [('agent', '0007_phase3')]
    operations = [migrations.RunPython(drop_baseline, migrations.RunPython.noop)]
