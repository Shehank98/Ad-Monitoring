from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0026_add_schedule_is_locked'),
    ]

    operations = [
        # ScheduleRow: MapOnline preliminary match fields
        migrations.AddField(
            model_name='schedulerow',
            name='is_maponline_matched',
            field=models.BooleanField(default=False, db_index=True),
        ),
        migrations.AddField(
            model_name='schedulerow',
            name='matched_maponline_lmrb',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='maponline_schedule_matches',
                to='core.lmrbrow',
            ),
        ),
        migrations.AddField(
            model_name='schedulerow',
            name='maponline_matched_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        # LMRBRow: MapOnline lock flag
        migrations.AddField(
            model_name='lmrbrow',
            name='is_maponline_schedule_matched',
            field=models.BooleanField(default=False, db_index=True),
        ),
        # BrandMapping: MapOnline theme field
        migrations.AddField(
            model_name='brandmapping',
            name='maponline_theme',
            field=models.TextField(
                blank=True,
                default='',
                help_text=(
                    'Theme name(s) as they appear in MapOnline files (Theme column). '
                    'Separate multiple values with a pipe: Theme A|Theme B. '
                    'Leave blank to skip this brand in MapOnline preliminary matching.'
                ),
            ),
        ),
    ]
