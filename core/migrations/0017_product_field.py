from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_brandmapping_tc_theme_textfield'),
    ]

    operations = [
        migrations.AddField(
            model_name='schedulerow',
            name='product',
            field=models.CharField(
                blank=True, default='', max_length=200,
                help_text='Product name — should match LMRB Product field',
            ),
        ),
        migrations.AddField(
            model_name='brandmapping',
            name='product',
            field=models.CharField(
                blank=True, default='', max_length=200,
                help_text='LMRB Product name this brand/theme belongs to (used for grouping in reports)',
            ),
        ),
    ]
