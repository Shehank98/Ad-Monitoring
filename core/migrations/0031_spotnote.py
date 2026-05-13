from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_systemsetting_display_category'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SpotNote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('mo', 'Marketing Officer'), ('ops', 'Operations')], max_length=10)),
                ('text', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='spot_notes', to=settings.AUTH_USER_MODEL)),
                ('schedule_row', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='spot_notes', to='core.schedulerow')),
            ],
            options={
                'ordering': ['schedule_row', 'role'],
                'unique_together': {('schedule_row', 'role')},
            },
        ),
    ]
