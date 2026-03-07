"""
0014 — Extend ManualMatch for 3-way TC/LMRB/Schedule manual reconciliation.

Changes:
  - schedule_row → nullable (OneToOneField null=True)
  - Add tc_row FK to TCRow (nullable OneToOneField)
  - Add match_mode CharField
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_manual_match'),
    ]

    operations = [
        # 1. Make schedule_row nullable so TC+LMRB-only matches are allowed
        migrations.AlterField(
            model_name='manualmatch',
            name='schedule_row',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='manual_match',
                to='core.schedulerow',
            ),
        ),
        # 2. Add match_mode (default keeps old records as 'schedule_lmrb')
        migrations.AddField(
            model_name='manualmatch',
            name='match_mode',
            field=models.CharField(
                choices=[
                    ('schedule_lmrb', 'Schedule + LMRB'),
                    ('3way', 'Schedule + TC + LMRB'),
                    ('tc_lmrb', 'TC + LMRB only'),
                ],
                default='schedule_lmrb',
                max_length=20,
            ),
        ),
        # 3. Add tc_row FK
        migrations.AddField(
            model_name='manualmatch',
            name='tc_row',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='manual_match',
                to='core.tcrow',
            ),
        ),
    ]
