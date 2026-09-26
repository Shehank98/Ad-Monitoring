from django.db import migrations


def clamp(apps, schema_editor):
    """Phase 2.1 item 7: the agent stays at level 0 until Phase 4 (also for a stored row)."""
    apps.get_model('agent', 'AgentConfig').objects.filter(autonomy_level__gt=0).update(autonomy_level=0)


class Migration(migrations.Migration):
    dependencies = [('agent', '0005_phase2_1')]
    operations = [migrations.RunPython(clamp, migrations.RunPython.noop)]
