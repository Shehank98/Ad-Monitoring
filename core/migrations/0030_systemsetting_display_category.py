from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0029_special_notes_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='systemsetting',
            name='category',
            field=models.CharField(
                choices=[
                    ('reconciliation', 'Reconciliation'),
                    ('tc_parsing',     'TC File Parsing'),
                    ('lmrb_parsing',   'LMRB / MapOnline File Parsing'),
                    ('whatsapp',       'WhatsApp Notifications'),
                    ('display',        'Display & Export'),
                ],
                default='reconciliation',
                max_length=50,
            ),
        ),
    ]
