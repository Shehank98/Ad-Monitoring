"""Materialize client-level access into per-brand assignments.

Client access used to be implicit: a user assigned to a Client could reach all
of that client's brands via a query-time union. That made it impossible to
remove a single brand from a user. Access is now per-brand only, so this
migration copies every existing client assignment into direct brand
assignments — no user loses access.
"""
from django.db import migrations


def materialize(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    Account = apps.get_model('core', 'Account')
    for user in User.objects.prefetch_related('clients').all():
        cids = [c.id for c in user.clients.all()]
        if cids:
            user.accounts.add(*Account.objects.filter(client_id__in=cids))


def noop(apps, schema_editor):
    # Reverse is a no-op: direct assignments simply remain in place.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_alter_user_role'),
        ('core', '0036_client_logo_summaryreportmeta_agency_pct_and_more'),
    ]

    operations = [
        migrations.RunPython(materialize, noop),
    ]
