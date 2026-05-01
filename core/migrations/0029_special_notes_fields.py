from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0028_add_client_model_and_user_clients'),
    ]

    operations = [
        migrations.AddField(
            model_name='account',
            name='enable_special_notes',
            field=models.BooleanField(
                default=False,
                help_text='Show the Special Notes / Cost Breakdown section on summary reports for this account.',
            ),
        ),
        migrations.AddField(
            model_name='summaryreportmeta',
            name='schedule_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True),
        ),
        migrations.AddField(
            model_name='summaryreportmeta',
            name='deviated_cost',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=15, null=True),
        ),
    ]
