from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_add_extra_aired_status'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='channelofficer',
            name='user',
            field=models.ForeignKey(
                settings.AUTH_USER_MODEL,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='channel_officer_profiles',
                null=True,
                blank=True,
                help_text='The system login account for this officer',
            ),
        ),
    ]
