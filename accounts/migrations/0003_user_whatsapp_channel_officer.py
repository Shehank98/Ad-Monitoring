from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='whatsapp_number',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Include country code, e.g. +94771234567',
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[
                    ('super_admin', 'Super Admin'),
                    ('admin', 'Admin'),
                    ('team_head', 'Team Head'),
                    ('planner', 'Planner'),
                    ('operations', 'Operations'),
                    ('channel_officer', 'Channel Officer'),
                ],
                default='planner',
                max_length=20,
            ),
        ),
    ]
