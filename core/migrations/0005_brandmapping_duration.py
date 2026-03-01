from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_add_brand_mapping"),
    ]

    operations = [
        # Remove old unique_together so we can allow (brand, theme) combos
        # that differ only by duration.
        migrations.AlterUniqueTogether(
            name="brandmapping",
            unique_together=set(),
        ),
        migrations.AddField(
            model_name="brandmapping",
            name="duration",
            field=models.PositiveIntegerField(
                null=True,
                blank=True,
                help_text="Duration in seconds (optional — leave blank to match any duration)",
            ),
        ),
        migrations.AlterModelOptions(
            name="brandmapping",
            options={"ordering": ["brand", "theme", "duration"]},
        ),
    ]
