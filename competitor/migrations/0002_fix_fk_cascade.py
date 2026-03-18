"""
Migration: add real ON DELETE CASCADE to the upload_batch FK on
competitor_competitorad so that deleting a batch from Railway's DB
UI (or any direct SQL) cascades to ad rows without a constraint violation.

Django's on_delete=CASCADE is Python-only; this migration wires up the
same behaviour at the PostgreSQL level.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('competitor', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            -- Drop the old FK constraint (no DB-level cascade)
            ALTER TABLE competitor_competitorad
              DROP CONSTRAINT IF EXISTS
                competitor_competito_upload_batch_id_ab92dc91_fk_competito;

            -- Re-add with ON DELETE CASCADE so Railway UI / direct SQL
            -- deletes also cascade to child rows automatically.
            ALTER TABLE competitor_competitorad
              ADD CONSTRAINT competitor_competito_upload_batch_id_ab92dc91_fk_competito
              FOREIGN KEY (upload_batch_id)
              REFERENCES competitor_competitoruploadbatch(id)
              ON DELETE CASCADE
              DEFERRABLE INITIALLY DEFERRED;
            """,
            reverse_sql="""
            -- Restore to Django default (no DB-level cascade)
            ALTER TABLE competitor_competitorad
              DROP CONSTRAINT IF EXISTS
                competitor_competito_upload_batch_id_ab92dc91_fk_competito;

            ALTER TABLE competitor_competitorad
              ADD CONSTRAINT competitor_competito_upload_batch_id_ab92dc91_fk_competito
              FOREIGN KEY (upload_batch_id)
              REFERENCES competitor_competitoruploadbatch(id)
              DEFERRABLE INITIALLY DEFERRED;
            """,
        ),
    ]
