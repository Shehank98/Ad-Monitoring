from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_sponsorship_assignment'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Add manual-match lock flag to ScheduleRow
        migrations.AddField(
            model_name='schedulerow',
            name='is_manual_matched',
            field=models.BooleanField(default=False, db_index=True),
        ),
        # 2. Add manual-match lock flag to LMRBRow
        migrations.AddField(
            model_name='lmrbrow',
            name='is_manual_matched',
            field=models.BooleanField(default=False, db_index=True),
        ),
        # 3. Create ManualMatch join table
        migrations.CreateModel(
            name='ManualMatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('channel',    models.CharField(max_length=200)),
                ('month',      models.CharField(max_length=50)),
                ('note',       models.TextField(blank=True, default='')),
                ('matched_at', models.DateTimeField(auto_now_add=True)),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='manual_matches',
                    to='core.account',
                )),
                ('lmrb_row', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='manual_match',
                    to='core.lmrbrow',
                )),
                ('matched_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='manual_matches',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('schedule_row', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='manual_match',
                    to='core.schedulerow',
                )),
            ],
            options={
                'ordering': ['-matched_at'],
            },
        ),
        # 4. Add 'manual_match' choice to MatchResult.status (CharField — no schema change needed,
        #    but document the addition via a state-only AlterField so makemigrations stays clean)
        migrations.AlterField(
            model_name='matchresult',
            name='status',
            field=models.CharField(
                choices=[
                    ('matched',            'Matched'),
                    ('programme_mismatch', 'Programme Mismatch'),
                    ('late_telecast',      'Late Telecast'),
                    ('not_aired',          'Not Aired'),
                    ('no_mapping',         'No Brand Mapping'),
                    ('manual_match',       'Manually Matched'),
                ],
                max_length=30,
            ),
        ),
    ]
