from django.db import migrations, models


def populate_channel_media_type(apps, schema_editor):
    """Auto-detect media_type for existing channels from their name prefix."""
    Channel = apps.get_model('core', 'Channel')
    prefixes = ['tv', 'radio', 'press']
    for ch in Channel.objects.all():
        parts = ch.name.split(' - ', 1)
        if len(parts) == 2 and parts[0].strip().lower() in prefixes:
            ch.media_type = parts[0].strip().lower()
        else:
            ch.media_type = 'other'
        ch.save(update_fields=['media_type'])


def populate_schedule_media_type(apps, schema_editor):
    """Auto-detect media_type for existing schedules from their channel name."""
    Schedule = apps.get_model('core', 'Schedule')
    prefixes = ['tv', 'radio', 'press']
    for s in Schedule.objects.all():
        parts = s.channel.split(' - ', 1)
        if len(parts) == 2 and parts[0].strip().lower() in prefixes:
            s.media_type = parts[0].strip().lower()
        else:
            s.media_type = 'other'
        s.save(update_fields=['media_type'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0020_schedule_makeup_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='channel',
            name='media_type',
            field=models.CharField(
                blank=True, default='', max_length=10,
                choices=[('tv', 'TV'), ('radio', 'Radio'), ('press', 'Press'), ('other', 'Other')],
                help_text='Detected from name prefix (TV - / Radio - / Press -)',
            ),
        ),
        migrations.AddField(
            model_name='schedule',
            name='media_type',
            field=models.CharField(
                blank=True, default='', max_length=10,
                choices=[('tv', 'TV'), ('radio', 'Radio'), ('press', 'Press'), ('other', 'Other')],
                help_text='TV / Radio / Press — auto-detected from channel name prefix at upload',
            ),
        ),
        migrations.RunPython(populate_channel_media_type, migrations.RunPython.noop),
        migrations.RunPython(populate_schedule_media_type, migrations.RunPython.noop),
    ]
