from django.db import models
from django.conf import settings
import hashlib
import uuid


class Channel(models.Model):
    """A TV/radio channel that admins can manage and planners select from."""
    name       = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Account(models.Model):
    """A brand/client account (e.g. Maliban, Dialog)."""
    name       = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Schedule(models.Model):
    """An ad schedule Excel file uploaded by a Planner.

    month, start_date, end_date and version are all auto-detected / auto-set
    from the uploaded file — planners no longer need to enter these manually.
    version auto-increments per (account, channel) so new uploads are trackable.
    """
    account           = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='schedules')
    channel           = models.CharField(max_length=200)
    month             = models.CharField(max_length=50)
    schedule_number   = models.CharField(max_length=50)
    file              = models.FileField(upload_to='schedules/')
    original_filename = models.CharField(max_length=255)
    row_count         = models.PositiveIntegerField(default=0)
    # Auto-detected from file
    start_date        = models.DateField(null=True, blank=True)
    end_date          = models.DateField(null=True, blank=True)
    # Version — auto-incremented per (account, channel) on each new upload
    version           = models.PositiveIntegerField(default=1)
    uploaded_by       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                          null=True, related_name='uploaded_schedules')
    uploaded_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.account} | {self.channel} | {self.month} | v{self.version} | #{self.schedule_number}'


class MonitoringData(models.Model):
    """A MapOnline or MediaWatch data file uploaded by Operations.

    LMRB / MapOnline files often contain multiple channels in a single sheet.
    The upload view auto-detects every unique channel and creates one
    MonitoringData record per channel, all pointing to the same physical file
    (identified by file_group_id).  start_date / end_date are also auto-detected.
    """
    DATA_TYPES = [
        ('maponline',  'MapOnline'),
        ('mediawatch', 'MediaWatch (LMRB)'),
    ]
    account           = models.ForeignKey(Account, on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name='monitoring_data')
    data_type         = models.CharField(max_length=20, choices=DATA_TYPES)
    channel           = models.CharField(max_length=200)
    start_date        = models.DateField(null=True, blank=True)
    end_date          = models.DateField(null=True, blank=True)
    file              = models.FileField(upload_to='monitoring/')
    original_filename = models.CharField(max_length=255)
    row_count         = models.PositiveIntegerField(default=0)
    # UUID shared by all channel-split records from the same physical upload
    file_group_id     = models.CharField(max_length=36, blank=True, default='')
    uploaded_by       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                          null=True, related_name='uploaded_monitoring')
    uploaded_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.get_data_type_display()} | {self.channel} | {self.start_date}–{self.end_date}'


class ScheduleRow(models.Model):
    """
    Individual row parsed from a Schedule Excel file.

    Stored in DB on upload so the matching engine can work with DB queries
    instead of reading Excel files.  is_matched / matched_lmrb implement the
    row-level locking that prevents double-counting across re-runs.
    """
    schedule    = models.ForeignKey(Schedule, on_delete=models.CASCADE, related_name='rows')
    account     = models.ForeignKey(Account, on_delete=models.CASCADE)
    channel     = models.CharField(max_length=200)
    month       = models.CharField(max_length=50)

    brand       = models.CharField(max_length=200)
    programme   = models.CharField(max_length=200, blank=True)
    date        = models.DateField(null=True, blank=True)
    start_time  = models.CharField(max_length=30, blank=True)
    end_time    = models.CharField(max_length=30, blank=True)
    duration    = models.IntegerField(null=True, blank=True)
    ad_type     = models.CharField(max_length=100, blank=True)   # 'COMMERCIAL BENEFITS' | 'SPONSORSHIP'

    # Row-level locking
    is_matched   = models.BooleanField(default=False, db_index=True)
    matched_lmrb = models.ForeignKey(
        'LMRBRow', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='schedule_matches',
    )
    matched_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['date', 'start_time']
        indexes = [
            models.Index(fields=['account', 'channel', 'month', 'is_matched']),
            models.Index(fields=['account', 'channel', 'month', 'ad_type']),
        ]

    def __str__(self):
        return f'{self.brand} | {self.date} | {self.start_time}–{self.end_time}'


class LMRBRow(models.Model):
    """
    Individual row from a monitoring (LMRB / MapOnline) file.

    All monitoring data lives in a single master table per account, deduplicated
    by (account, channel, date, advt_time, advt_theme, duration).  Uploading a
    new file whose rows match existing entries replaces those entries (the old
    record and its matched state are cleared first).
    """
    account     = models.ForeignKey(Account, on_delete=models.CASCADE)
    channel     = models.CharField(max_length=200)
    date        = models.DateField()
    advt_theme  = models.CharField(max_length=500)
    advt_time   = models.CharField(max_length=30)
    duration    = models.IntegerField(null=True, blank=True)
    source      = models.CharField(max_length=20)   # 'maponline' | 'mediawatch'

    # ── Extended LMRB columns ─────────────────────────────────────────────────
    product_group = models.CharField(max_length=500, blank=True, default='')
    advertiser    = models.CharField(max_length=500, blank=True, default='')
    product       = models.CharField(max_length=500, blank=True, default='')
    ads           = models.CharField(max_length=500, blank=True, default='')
    program       = models.CharField(max_length=500, blank=True, default='')   # aired programme
    prog_time     = models.CharField(max_length=30,  blank=True, default='')
    ad_pos        = models.IntegerField(null=True, blank=True)
    tot_ads       = models.IntegerField(null=True, blank=True)
    brk_no        = models.IntegerField(null=True, blank=True)
    pos_in_brk    = models.IntegerField(null=True, blank=True)
    ads_in_brk    = models.IntegerField(null=True, blank=True)
    lng           = models.CharField(max_length=100, blank=True, default='')
    cost          = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    day           = models.CharField(max_length=20, blank=True, default='')

    # Dedup key = sha256(account_id|channel|date|advt_time|advt_theme|dur)[:32]
    # Unique so duplicate uploads just replace the row.
    dedup_key   = models.CharField(max_length=64, unique=True)

    # Row-level locking
    is_matched       = models.BooleanField(default=False, db_index=True)
    matched_schedule = models.ForeignKey(
        ScheduleRow, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='lmrb_matches',
    )
    matched_at       = models.DateTimeField(null=True, blank=True)

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'advt_time']
        indexes = [
            models.Index(fields=['account', 'channel', 'date', 'is_matched']),
        ]

    def __str__(self):
        return f'{self.channel} | {self.advt_theme} | {self.date} | {self.advt_time}'

    @staticmethod
    def make_dedup_key(account_id, channel, date, advt_time, advt_theme, dur):
        raw = f'{account_id}|{channel}|{date}|{advt_time}|{advt_theme}|{dur}'
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class BrandMapping(models.Model):
    """Maps a schedule Brand name to a monitoring Advt_Theme/Theme name, per account.
    One brand can map to many themes (one row per brand-theme pair).
    Duration is optional: when set, the mapping only applies to ads with that duration.
    """
    account  = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='brand_mappings')
    brand    = models.CharField(max_length=200, help_text='Brand name as it appears in the Schedule file')
    theme    = models.CharField(max_length=200, help_text='Theme name as it appears in LMRB (Advt_Theme) or MapOnline (Theme)')
    duration = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Duration in seconds (optional — leave blank to match any duration)',
    )

    class Meta:
        ordering = ['brand', 'theme', 'duration']

    def __str__(self):
        dur_str = f' ({self.duration}s)' if self.duration is not None else ''
        return f'{self.account.name}: {self.brand} → {self.theme}{dur_str}'


class MatchResult(models.Model):
    """Persisted result of one matched (or unmatched) schedule ad row.

    Stored after every verification run. Enables:
    - Smart re-run (skip already-Matched rows, reuse locked LMRB slots)
    - Full audit trail / history
    - Dashboard analytics without re-running the engine
    """
    STATUS_CHOICES = [
        ('matched',            'Matched'),
        ('programme_mismatch', 'Programme Mismatch'),
        ('late_telecast',      'Late Telecast'),
        ('not_aired',          'Not Aired'),
        ('no_mapping',         'No Brand Mapping'),
    ]
    account          = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='match_results')
    channel          = models.CharField(max_length=200)
    month            = models.CharField(max_length=50)

    # ── Schedule row ────────────────────────────────────────────────────────
    brand            = models.CharField(max_length=200)
    programme        = models.CharField(max_length=200, blank=True)
    scheduled_date   = models.DateField(null=True, blank=True)
    planned_start    = models.CharField(max_length=20, blank=True)
    planned_end      = models.CharField(max_length=20, blank=True)
    duration         = models.IntegerField(null=True, blank=True)

    # ── Matched monitoring row ───────────────────────────────────────────────
    theme            = models.CharField(max_length=500, blank=True)
    aired_date       = models.DateField(null=True, blank=True)
    air_time         = models.CharField(max_length=20, blank=True)
    source           = models.CharField(max_length=20, blank=True)

    # ── Result ──────────────────────────────────────────────────────────────
    status           = models.CharField(max_length=30, choices=STATUS_CHOICES)

    # Fingerprint of the consumed LMRB row — kept for backward compatibility.
    lmrb_fingerprint = models.CharField(max_length=64, blank=True)

    # Optional FK links to row-level records (populated by the DB-based engine).
    schedule_row = models.ForeignKey(ScheduleRow, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='match_results')
    lmrb_row     = models.ForeignKey(LMRBRow, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='match_results')

    run_at           = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-run_at', 'brand', 'scheduled_date']

    def __str__(self):
        return f'{self.account} | {self.channel} | {self.brand} | {self.scheduled_date} | {self.status}'
