import hashlib

from django.db import migrations, models


def recalculate_dedup_keys(apps, schema_editor):
    """
    Recalculate all existing LMRBRow dedup_keys using the new extended formula
    (now includes brk_no, pos_in_brk, advertiser, product).

    Rows whose new key collides with another row (true duplicates by the stricter
    formula) are deleted; the first occurrence (lowest id) is kept.
    """
    LMRBRow = apps.get_model('core', 'LMRBRow')

    rows = list(LMRBRow.objects.all().order_by('id'))
    seen_keys = {}   # new_key → id of first row with this key
    to_delete = []
    to_update = []

    for row in rows:
        raw = (
            f'{row.account_id}|{row.channel}|{row.date}|{row.advt_time}|'
            f'{row.advt_theme}|{row.duration}|'
            f'{row.brk_no or ""}|{row.pos_in_brk or ""}|'
            f'{row.advertiser or ""}|{row.product or ""}'
        )
        new_key = hashlib.sha256(raw.encode()).hexdigest()[:32]

        if new_key in seen_keys:
            to_delete.append(row.id)
        else:
            seen_keys[new_key] = row.id
            row.dedup_key = new_key
            to_update.append(row)

    if to_delete:
        LMRBRow.objects.filter(id__in=to_delete).delete()

    if to_update:
        LMRBRow.objects.bulk_update(to_update, ['dedup_key'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_system_setting'),
    ]

    operations = [
        # 1. Add batch_id field
        migrations.AddField(
            model_name='lmrbrow',
            name='batch_id',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        # 2. Recalculate dedup_keys with extended formula
        migrations.RunPython(
            recalculate_dedup_keys,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
