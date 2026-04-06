from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0025_channelofficer_user_fk'),
    ]

    operations = [
        migrations.AddField(
            model_name='schedule',
            name='is_locked',
            field=models.BooleanField(
                default=False,
                help_text='Locked by operations after confirming all LMRB spots are verified.',
            ),
        ),
    ]
