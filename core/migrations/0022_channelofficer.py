from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0021_media_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ChannelOfficer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel', models.CharField(max_length=200)),
                ('name', models.CharField(max_length=150)),
                ('whatsapp_number', models.CharField(
                    blank=True,
                    default='',
                    help_text='Include country code, e.g. +94771234567',
                    max_length=20,
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='channel_officers',
                    to='core.account',
                )),
                ('user', models.OneToOneField(
                    blank=True,
                    help_text='The system login account for this officer',
                    null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='channel_officer_profile',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['account', 'channel', 'name'],
                'unique_together': {('account', 'channel', 'name')},
            },
        ),
    ]
