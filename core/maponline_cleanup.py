"""
MapOnline data retention.

MapOnline monitoring data is used only as a view-only preliminary check on the
Verify Ads page — it never feeds the authoritative MediaWatch reconciliation,
the Summary Sheet, PDFs, or the Monitoring Dashboard.  Because it is disposable
and can be large, we do not keep it around: anything older than the retention
window (default 30 days, measured by upload date) is purged.

Called:
- automatically after every MapOnline upload (core.views.monitoring_upload), and
- on demand via the ``purge_maponline`` management command (for deploy/cron).
"""
import uuid as _uuid
from datetime import timedelta

from django.core.files.storage import default_storage
from django.utils import timezone

from core.models import LMRBRow, MonitoringData


def purge_old_maponline_data(days=30):
    """Delete MapOnline monitoring data older than ``days`` (by upload date).

    Removes the MonitoringData header records, their LMRBRow observations
    (source='maponline'), and the underlying uploaded files.  MediaWatch data is
    never touched.  Returns a summary dict of what was removed.
    """
    cutoff = timezone.now() - timedelta(days=days)

    old_md = MonitoringData.objects.filter(
        data_type='maponline', uploaded_at__lt=cutoff,
    )

    # Collect batch ids (to remove the exact LMRB rows) and file paths.
    batch_ids = set()
    files_to_delete = set()
    for md in old_md:
        if md.file_group_id:
            try:
                batch_ids.add(_uuid.UUID(md.file_group_id))
            except (ValueError, TypeError):
                pass
        if md.file:
            files_to_delete.add(md.file.name)

    lmrb_deleted = 0

    # 1) Rows tied to the expiring upload batches.
    if batch_ids:
        batch_qs = LMRBRow.objects.filter(source='maponline', batch_id__in=batch_ids)
        lmrb_deleted += batch_qs.count()
        batch_qs.delete()

    # 2) Catch-all: any stray MapOnline rows whose own upload time is past the
    #    window (older uploads that predate batch tracking, or orphans).
    stray_qs = LMRBRow.objects.filter(source='maponline', uploaded_at__lt=cutoff)
    lmrb_deleted += stray_qs.count()
    stray_qs.delete()

    md_deleted = old_md.count()
    old_md.delete()

    # Remove the physical files last (DB rows are already gone).  Channel-split
    # records share one file via file_group_id, so the set dedups them.
    files_removed = 0
    for name in files_to_delete:
        try:
            if name and default_storage.exists(name):
                default_storage.delete(name)
                files_removed += 1
        except Exception:
            # Missing/locked files must never break the purge.
            pass

    return {
        'days': days,
        'monitoring_deleted': md_deleted,
        'lmrb_deleted': lmrb_deleted,
        'files_removed': files_removed,
    }
