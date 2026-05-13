from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0031_spotnote'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Add unread_for_recipient to SpotNote
        migrations.AddField(
            model_name='spotnote',
            name='unread_for_recipient',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'True when the note was just saved and the other role has not yet seen it. '
                    'Cleared when the recipient opens the relevant page.'
                ),
            ),
        ),

        # Create SpotNotification
        migrations.CreateModel(
            name='SpotNotification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('message', models.TextField()),
                ('is_read', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('recipient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='spot_notifications',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('schedule_row', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='notifications',
                    to='core.schedulerow',
                )),
                ('created_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_notifications',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
