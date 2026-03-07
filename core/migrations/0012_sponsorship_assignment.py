from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_lmrbrow_batch_id_dedup'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # 1. Add sponsorship lock flag to LMRBRow
        migrations.AddField(
            model_name='lmrbrow',
            name='is_sponsorship_matched',
            field=models.BooleanField(default=False, db_index=True),
        ),
        # 2. Create SponsorshipLmrbAssignment join table
        migrations.CreateModel(
            name='SponsorshipLmrbAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('match_type', models.CharField(
                    choices=[('auto', 'Auto (Step 1)'), ('manual', 'Manual (Step 2)')],
                    default='auto', max_length=10,
                )),
                ('matched_at', models.DateTimeField(auto_now_add=True)),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sponsorship_assignments',
                    to='core.account',
                )),
                ('lmrb_row', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sponsorship_assignment',
                    to='core.lmrbrow',
                )),
                ('matched_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='sponsorship_assignments',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('schedule_row', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sponsorship_assignment',
                    to='core.schedulerow',
                )),
            ],
            options={
                'ordering': ['schedule_row__date', 'schedule_row__start_time'],
            },
        ),
    ]
