from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_tc_transmission_report'),
    ]

    operations = [
        migrations.CreateModel(
            name='SystemSetting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('key',         models.CharField(max_length=100, unique=True)),
                ('value',       models.TextField(blank=True, default='')),
                ('label',       models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('category',    models.CharField(
                    choices=[
                        ('reconciliation', 'Reconciliation'),
                        ('tc_parsing',     'TC File Parsing'),
                        ('lmrb_parsing',   'LMRB / MapOnline File Parsing'),
                    ],
                    default='reconciliation',
                    max_length=50,
                )),
                ('updated_at',  models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['category', 'key'],
            },
        ),
    ]
