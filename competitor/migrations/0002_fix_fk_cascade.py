"""
Migration: add real ON DELETE CASCADE to the upload_batch FK on
competitor_competitorad so that deleting a batch from Railway's DB
UI (or any direct SQL) cascades to ad rows without a constraint violation.

Django's on_delete=CASCADE is Python-only; this migration wires up the
same behaviour at the PostgreSQL level.
"""
from django.db import migrations


# The DB-level ON DELETE CASCADE fix is PostgreSQL-specific (production on
# Railway). On SQLite (dev DB per CLAUDE.md, and the test runner) the ALTER
# TABLE ... ADD CONSTRAINT syntax is invalid and Django's Python-level
# on_delete=CASCADE already covers deletes, so this migration is a no-op there.
FORWARD_SQL = """
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
"""

REVERSE_SQL = """
-- Restore to Django default (no DB-level cascade)
ALTER TABLE competitor_competitorad
  DROP CONSTRAINT IF EXISTS
    competitor_competito_upload_batch_id_ab92dc91_fk_competito;

ALTER TABLE competitor_competitorad
  ADD CONSTRAINT competitor_competito_upload_batch_id_ab92dc91_fk_competito
  FOREIGN KEY (upload_batch_id)
  REFERENCES competitor_competitoruploadbatch(id)
  DEFERRABLE INITIALLY DEFERRED;
"""


def _forward(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(FORWARD_SQL)


def _reverse(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):

    dependencies = [
        ('competitor', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(_forward, _reverse),
    ]
