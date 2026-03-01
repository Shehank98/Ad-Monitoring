from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_brandmapping_duration"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── Schedule: version + auto-detected date range ─────────────────────
        migrations.AddField(
            model_name="schedule",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="schedule",
            name="start_date",
            field=models.DateField(
                null=True, blank=True,
                help_text="Auto-detected from schedule file (Date column)",
            ),
        ),
        migrations.AddField(
            model_name="schedule",
            name="end_date",
            field=models.DateField(
                null=True, blank=True,
                help_text="Auto-detected from schedule file (Date column)",
            ),
        ),

        # ── MonitoringData: file_group_id + make dates nullable ───────────────
        migrations.AddField(
            model_name="monitoringdata",
            name="file_group_id",
            field=models.CharField(
                max_length=36, blank=True, default="",
                help_text="UUID shared by all channel-split records from the same physical upload",
            ),
        ),
        migrations.AlterField(
            model_name="monitoringdata",
            name="start_date",
            field=models.DateField(null=True, blank=True),
        ),
        migrations.AlterField(
            model_name="monitoringdata",
            name="end_date",
            field=models.DateField(null=True, blank=True),
        ),

        # ── MatchResult: new model ─────────────────────────────────────────────
        migrations.CreateModel(
            name="MatchResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="match_results",
                        to="core.account",
                    ),
                ),
                ("channel",        models.CharField(max_length=200)),
                ("month",          models.CharField(max_length=50)),
                ("brand",          models.CharField(max_length=200)),
                ("programme",      models.CharField(max_length=200, blank=True)),
                ("scheduled_date", models.DateField(null=True, blank=True)),
                ("planned_start",  models.CharField(max_length=20, blank=True)),
                ("planned_end",    models.CharField(max_length=20, blank=True)),
                ("duration",       models.IntegerField(null=True, blank=True)),
                ("theme",          models.CharField(max_length=500, blank=True)),
                ("aired_date",     models.DateField(null=True, blank=True)),
                ("air_time",       models.CharField(max_length=20, blank=True)),
                ("source",         models.CharField(max_length=20, blank=True)),
                (
                    "status",
                    models.CharField(
                        max_length=30,
                        choices=[
                            ("matched",            "Matched"),
                            ("programme_mismatch", "Programme Mismatch"),
                            ("late_telecast",      "Late Telecast"),
                            ("not_aired",          "Not Aired"),
                            ("no_mapping",         "No Brand Mapping"),
                        ],
                    ),
                ),
                ("lmrb_fingerprint", models.CharField(max_length=64, blank=True)),
                ("run_at",           models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-run_at", "brand", "scheduled_date"]},
        ),
    ]
