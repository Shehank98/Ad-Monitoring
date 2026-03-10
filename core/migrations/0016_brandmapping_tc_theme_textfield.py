from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_schedule_is_superseded'),
    ]

    operations = [
        migrations.AlterField(
            model_name='brandmapping',
            name='tc_theme',
            field=models.TextField(
                blank=True,
                default='',
                help_text=(
                    'Theme/Product name(s) as they appear in the Transmission Certificate (TC) file. '
                    'Separate multiple TC theme names with a pipe character: Theme A|Theme B|Theme C'
                ),
            ),
        ),
    ]
