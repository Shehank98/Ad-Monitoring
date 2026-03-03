"""
Management command: fix_lmrb_channels

Renames LMRBRow records whose channel field was stored with the LMRB medium
prefix ("Tv - Sirasa TV", "Radio - Sirasa FM") to the bare channel name used
by Schedule and TC records ("Sirasa TV", "Sirasa FM").

This is a one-time fix for data uploaded before the _canon_channel function
was updated to strip the prefix automatically on upload.

Usage:
    python manage.py fix_lmrb_channels                # dry-run (no DB changes)
    python manage.py fix_lmrb_channels --apply        # actually update the DB
"""
import hashlib

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Channel, LMRBRow


_PREFIXES = ('tv - ', 'radio - ')


def _strip_prefix(channel_name: str) -> str | None:
    """Return the name without the LMRB prefix, or None if no prefix found."""
    for prefix in _PREFIXES:
        if channel_name.lower().startswith(prefix):
            return channel_name[len(prefix):]
    return None


def _make_dedup_key(account_id, channel, date, advt_time, advt_theme, duration) -> str:
    raw = f'{account_id}|{channel}|{date}|{advt_time}|{advt_theme}|{duration}'
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Command(BaseCommand):
    help = (
        'Fix LMRBRow records whose channel was stored with the LMRB medium prefix '
        '("Tv - Sirasa TV" → "Sirasa TV").  Run without --apply for a dry-run.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            default=False,
            help='Commit changes to the database (default: dry-run only).',
        )

    def handle(self, *args, **options):
        apply = options['apply']
        mode = 'APPLY' if apply else 'DRY-RUN'
        self.stdout.write(f'[fix_lmrb_channels] Mode: {mode}')

        # Build canonical channel lookup from Channel model
        ch_canonical = {c.lower(): c for c in Channel.objects.values_list('name', flat=True)}
        self.stdout.write(f'[fix_lmrb_channels] {len(ch_canonical)} canonical channel(s) in Channel model')

        # Find all LMRBRow records with prefixed channel names
        prefixed_qs = LMRBRow.objects.filter(channel__iregex=r'^(Tv|Radio|TV|RADIO) - ')
        total_prefixed = prefixed_qs.count()
        self.stdout.write(f'[fix_lmrb_channels] {total_prefixed} LMRBRow record(s) have a prefixed channel name')

        if total_prefixed == 0:
            self.stdout.write(self.style.SUCCESS('Nothing to fix.'))
            return

        # Preview the channel renames
        channel_map = {}   # old_name → new_name
        for old_ch in prefixed_qs.values_list('channel', flat=True).distinct():
            stripped = _strip_prefix(old_ch)
            if stripped is None:
                continue
            # Prefer canonical casing if available in Channel model
            new_ch = ch_canonical.get(stripped.lower(), stripped)
            channel_map[old_ch] = new_ch
            self.stdout.write(f'  {repr(old_ch):40s} → {repr(new_ch)}')

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    f'\nDry-run complete. {total_prefixed} record(s) would be updated. '
                    'Re-run with --apply to commit changes.'
                )
            )
            return

        # Apply the renames in a transaction
        updated = 0
        skipped_dup = 0
        with transaction.atomic():
            for old_ch, new_ch in channel_map.items():
                rows = list(LMRBRow.objects.filter(channel=old_ch))
                for row in rows:
                    new_key = _make_dedup_key(
                        row.account_id, new_ch, row.date,
                        row.advt_time, row.advt_theme, row.duration,
                    )
                    # Skip if a row with the new dedup_key already exists
                    if LMRBRow.objects.filter(dedup_key=new_key).exclude(pk=row.pk).exists():
                        self.stdout.write(
                            self.style.WARNING(
                                f'  Skipping pk={row.pk} — new dedup_key already exists'
                            )
                        )
                        skipped_dup += 1
                        continue

                    row.channel   = new_ch
                    row.dedup_key = new_key
                    row.save(update_fields=['channel', 'dedup_key'])
                    updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'\nDone. Updated {updated} record(s). Skipped {skipped_dup} duplicate(s).'
            )
        )
