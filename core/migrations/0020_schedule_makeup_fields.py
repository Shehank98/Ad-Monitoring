from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0019_add_sponsorship_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='schedule',
            name='makeup_end_date',
            field=models.DateField(
                blank=True, null=True,
                help_text='If spots may air after the schedule ends, set this date as the latest '
                          'date to look for LMRB data (e.g. Feb 10 for a Jan schedule with makeup spots).',
            ),
        ),
        migrations.AddField(
            model_name='schedule',
            name='parent_schedule',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='makeup_schedules',
                to='core.schedule',
                help_text='Link to the original schedule if this is a makeup/reschedule uploaded '
                          'for missed spots from a previous month.',
            ),
        ),
    ]
