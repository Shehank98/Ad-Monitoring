from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_product_field'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[
                    ('settings_change', 'Settings Changed'),
                    ('db_reset',        'DB Reset'),
                    ('db_delete',       'DB Delete'),
                    ('db_backup',       'DB Backup'),
                    ('db_dedup',        'Duplicate Removal'),
                    ('user_create',     'User Created'),
                    ('user_toggle',     'User Activated/Deactivated'),
                    ('user_pwd_reset',  'Password Reset'),
                    ('user_accounts',   'User Accounts Updated'),
                ], max_length=50)),
                ('detail', models.TextField(blank=True, default='')),
                ('ip', models.GenericIPAddressField(blank=True, null=True)),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='audit_logs',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-timestamp'],
            },
        ),
    ]
