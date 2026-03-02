from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_versioning_channels_matchresult'),
    ]

    operations = [
        # ── ScheduleRow ────────────────────────────────────────────────────────
        migrations.CreateModel(
            name='ScheduleRow',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel',     models.CharField(max_length=200)),
                ('month',       models.CharField(max_length=50)),
                ('brand',       models.CharField(max_length=200)),
                ('programme',   models.CharField(blank=True, max_length=200)),
                ('date',        models.DateField(blank=True, null=True)),
                ('start_time',  models.CharField(blank=True, max_length=30)),
                ('end_time',    models.CharField(blank=True, max_length=30)),
                ('duration',    models.IntegerField(blank=True, null=True)),
                ('ad_type',     models.CharField(blank=True, max_length=100)),
                ('is_matched',  models.BooleanField(db_index=True, default=False)),
                ('matched_at',  models.DateTimeField(blank=True, null=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='core.account')),
                ('schedule', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rows', to='core.schedule')),
                # matched_lmrb added after LMRBRow is created (see below)
            ],
            options={
                'ordering': ['date', 'start_time'],
            },
        ),
        migrations.AddIndex(
            model_name='schedulerow',
            index=models.Index(fields=['account', 'channel', 'month', 'is_matched'], name='core_schedu_account_idx1'),
        ),
        migrations.AddIndex(
            model_name='schedulerow',
            index=models.Index(fields=['account', 'channel', 'month', 'ad_type'], name='core_schedu_account_idx2'),
        ),

        # ── LMRBRow ────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name='LMRBRow',
            fields=[
                ('id',          models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel',     models.CharField(max_length=200)),
                ('date',        models.DateField()),
                ('advt_theme',  models.CharField(max_length=500)),
                ('advt_time',   models.CharField(max_length=30)),
                ('duration',    models.IntegerField(blank=True, null=True)),
                ('source',      models.CharField(max_length=20)),
                ('dedup_key',   models.CharField(max_length=64, unique=True)),
                ('is_matched',  models.BooleanField(db_index=True, default=False)),
                ('matched_at',  models.DateTimeField(blank=True, null=True)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='core.account')),
                ('matched_schedule', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='lmrb_matches', to='core.schedulerow',
                )),
            ],
            options={
                'ordering': ['date', 'advt_time'],
            },
        ),
        migrations.AddIndex(
            model_name='lmrbrow',
            index=models.Index(fields=['account', 'channel', 'date', 'is_matched'], name='core_lmrbro_account_idx1'),
        ),

        # ── Add matched_lmrb FK back to ScheduleRow ───────────────────────────
        migrations.AddField(
            model_name='schedulerow',
            name='matched_lmrb',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='schedule_matches', to='core.lmrbrow',
            ),
        ),

        # ── MatchResult: add FK links to row-level records ─────────────────────
        migrations.AddField(
            model_name='matchresult',
            name='schedule_row',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='match_results', to='core.schedulerow',
            ),
        ),
        migrations.AddField(
            model_name='matchresult',
            name='lmrb_row',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='match_results', to='core.lmrbrow',
            ),
        ),
    ]
