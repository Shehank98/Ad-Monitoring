"""Management command: purge MapOnline monitoring data older than N days.

Usage:
    python manage.py purge_maponline            # 30-day default
    python manage.py purge_maponline --days 45
    python manage.py purge_maponline --dry-run  # report only, delete nothing

Intended to run on deploy or a scheduled job so old MapOnline data is removed
even when no new MapOnline files are being uploaded.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from core.maponline_cleanup import purge_old_maponline_data
from core.models import LMRBRow, MonitoringData


class Command(BaseCommand):
    help = 'Delete MapOnline monitoring data older than N days (default 30, by upload date).'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30,
                            help='Retention window in days (default: 30).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be deleted without deleting anything.')

    def handle(self, *args, **options):
        days = options['days']
        if options['dry_run']:
            cutoff = timezone.now() - timedelta(days=days)
            md = MonitoringData.objects.filter(data_type='maponline', uploaded_at__lt=cutoff).count()
            lmrb = LMRBRow.objects.filter(source='maponline', uploaded_at__lt=cutoff).count()
            self.stdout.write(self.style.WARNING(
                f'[dry-run] Would remove {md} MapOnline upload record(s) and '
                f'~{lmrb} LMRB row(s) older than {days} days.'
            ))
            return

        result = purge_old_maponline_data(days=days)
        self.stdout.write(self.style.SUCCESS(
            f'Purged MapOnline data older than {result["days"]} days: '
            f'{result["monitoring_deleted"]} upload record(s), '
            f'{result["lmrb_deleted"]} LMRB row(s), '
            f'{result["files_removed"]} file(s) removed.'
        ))
