from django.db import models
from django.conf import settings
import hashlib
import uuid


MEDIA_TYPE_CHOICES = [
    ('tv',    'TV'),
    ('radio', 'Radio'),
    ('press', 'Press'),
    ('other', 'Other'),
]

_MEDIA_PREFIXES = ['tv', 'radio', 'press']


def parse_channel_media_type(name: str):
    """Parse 'TV - Sirasa TV' → ('tv', 'Sirasa TV').
    Returns (media_type, clean_name).  clean_name has the 'TYPE - ' prefix stripped.
    Unrecognised prefix → ('other', original_name).
    """
    parts = name.split(' - ', 1)
    if len(parts) == 2:
        prefix = parts[0].strip().lower()
        if prefix in _MEDIA_PREFIXES:
            return prefix, parts[1].strip()
    return 'other', name.strip()


class Channel(models.Model):
    """A TV/radio channel that admins can manage and planners select from."""
    name       = models.CharField(max_length=200, unique=True)
    media_type = models.CharField(
        max_length=10, choices=MEDIA_TYPE_CHOICES, blank=True, default='',
        help_text='Detected from name prefix (TV - / Radio - / Press -)',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.media_type:
            self.media_type, _ = parse_channel_media_type(self.name)
        super().save(*args, **kwargs)


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
    media_type        = models.CharField(
        max_length=10, choices=MEDIA_TYPE_CHOICES, blank=True, default='',
        help_text='TV / Radio / Press — auto-detected from channel name prefix at upload',
    )
    month             = models.CharField(max_length=50)
    schedule_number   = models.CharField(max_length=50)
    file              = models.FileField(upload_to='schedules/')
    original_filename = models.CharField(max_length=255)
    row_count         = models.PositiveIntegerField(default=0)
    # Auto-detected from file
    start_date        = models.DateField(null=True, blank=True)
    end_date          = models.DateField(null=True, blank=True)
    # Version — auto-incremented per (account, channel, month) on each new upload
    version           = models.PositiveIntegerField(default=1)
    # Superseded by a later replacement upload with the same schedule_number
    is_superseded     = models.BooleanField(default=False)
    # Makeup / late-airing support
    # If set, the LMRB pool extends to this date when reconciling this schedule,
    # allowing Late Telecast matching to find spots aired after the schedule period.
    makeup_end_date   = models.DateField(
        null=True, blank=True,
        help_text='If spots may air after the schedule ends, set this date as the latest '
                  'date to look for LMRB data (e.g. Feb 10 for a Jan schedule with makeup spots).',
    )
    # If this schedule is a formal makeup/reschedule uploaded under a different month,
    # link it to the original schedule so its rows are reconciled under the parent month.
    parent_schedule   = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='makeup_schedules',
        help_text='Link to the original schedule if this is a makeup/reschedule uploaded '
                  'for missed spots from a previous month.',
    )
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
    product     = models.CharField(max_length=200, blank=True, default='',
                    help_text='Product name — should match LMRB Product field')
    programme   = models.CharField(max_length=200, blank=True)
    date        = models.DateField(null=True, blank=True)
    start_time  = models.CharField(max_length=30, blank=True)
    end_time    = models.CharField(max_length=30, blank=True)
    duration    = models.IntegerField(null=True, blank=True)
    ad_type          = models.CharField(max_length=100, blank=True)   # 'COMMERCIAL BENEFITS' | 'SPONSORSHIP'
    sponsorship_type = models.CharField(max_length=100, blank=True, default='',
                         help_text='Type of sponsorship e.g. BB, Tag, Opening/Closing — set at upload')

    # Row-level locking — commercial reconciliation
    is_matched   = models.BooleanField(default=False, db_index=True)
    matched_lmrb = models.ForeignKey(
        'LMRBRow', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='schedule_matches',
    )
    matched_at   = models.DateTimeField(null=True, blank=True)

    # Manual reconciliation lock — set when a ManualMatch record is created for
    # this row.  Prevents the engine from counting it as Not Aired and stops it
    # from being re-processed in future auto runs.
    is_manual_matched = models.BooleanField(default=False, db_index=True)

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

    All monitoring uploads append to one master table per account.  Rows are
    deduplicated on upload using a SHA-256 key of all meaningful columns so
    uploading the same file twice produces no duplicates, while uploading a
    different date range correctly adds new rows without touching existing ones.

    batch_id: UUID shared by all rows from the same upload batch
    (same as the MonitoringData.file_group_id).  Used to delete a batch cleanly.
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

    # Dedup key — SHA-256 of all meaningful columns.
    # Uploading the same file twice skips existing rows (append-only, no replace).
    dedup_key   = models.CharField(max_length=64, unique=True)

    # Upload batch identifier — same UUID as MonitoringData.file_group_id so that
    # deleting a MonitoringData group also removes all its LMRBRows.
    batch_id    = models.UUIDField(null=True, blank=True, db_index=True)

    # Row-level locking — commercial reconciliation
    is_matched       = models.BooleanField(default=False, db_index=True)
    matched_schedule = models.ForeignKey(
        ScheduleRow, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='lmrb_matches',
    )
    matched_at       = models.DateTimeField(null=True, blank=True)

    # Sponsorship reconciliation lock (set after Step 1 auto or Step 2 manual)
    is_sponsorship_matched = models.BooleanField(default=False, db_index=True)

    # Manual reconciliation lock — permanently locked once a ManualMatch is created
    is_manual_matched = models.BooleanField(default=False, db_index=True)

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'advt_time']
        indexes = [
            models.Index(fields=['account', 'channel', 'date', 'is_matched']),
        ]

    def __str__(self):
        return f'{self.channel} | {self.advt_theme} | {self.date} | {self.advt_time}'

    @staticmethod
    def make_dedup_key(account_id, channel, date, advt_time, advt_theme, dur,
                       brk_no=None, pos_in_brk=None, advertiser='', product=''):
        """
        Build a SHA-256 dedup key from all meaningful identifying columns.
        Including brk_no + pos_in_brk ensures two spots in the same break
        (same time/theme/duration) are treated as distinct rows.
        """
        raw = (f'{account_id}|{channel}|{date}|{advt_time}|{advt_theme}|{dur}|'
               f'{brk_no or ""}|{pos_in_brk or ""}|{advertiser or ""}|{product or ""}')
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class BrandMapping(models.Model):
    """Maps a schedule Brand name to monitoring/TC Theme names, per account.
    One brand can map to many themes (one row per brand-theme pair).
    Duration is optional: when set, the mapping only applies to ads with that duration.
    tc_theme maps the brand to how it appears in Transmission Certificate (TC) files.
    """
    account   = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='brand_mappings')
    product   = models.CharField(max_length=200, blank=True, default='',
                  help_text='LMRB Product name this brand/theme belongs to (used for grouping in reports)')
    brand     = models.CharField(max_length=200, help_text='Brand name as it appears in the Schedule file')
    theme     = models.CharField(max_length=200, help_text='Theme name as it appears in LMRB (Advt_Theme) or MapOnline (Theme)')
    tc_theme  = models.TextField(
        blank=True, default='',
        help_text=(
            'Theme/Product name(s) as they appear in the Transmission Certificate (TC) file. '
            'Separate multiple TC theme names with a pipe character: Theme A|Theme B|Theme C'
        ),
    )
    duration  = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Duration in seconds (optional — leave blank to match any duration)',
    )

    class Meta:
        ordering = ['brand', 'theme', 'duration']

    def __str__(self):
        dur_str = f' ({self.duration}s)' if self.duration is not None else ''
        return f'{self.account.name}: {self.brand} → {self.theme}{dur_str}'

    @property
    def tc_themes_list(self):
        """Return tc_theme split by pipe as a list, stripping blanks."""
        if not self.tc_theme:
            return []
        return [t.strip() for t in self.tc_theme.split('|') if t.strip()]


class TransmissionReport(models.Model):
    """A Transmission Certificate (TC) file received from the channel after a schedule period ends.
    Contains the channel's own record of every ad spot that actually aired.
    """
    account           = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='tc_reports')
    channel           = models.CharField(max_length=200)
    month             = models.CharField(max_length=50)
    schedule          = models.ForeignKey(
        'Schedule', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='tc_reports',
    )
    file              = models.FileField(upload_to='tc/')
    original_filename = models.CharField(max_length=255)
    row_count         = models.PositiveIntegerField(default=0)
    start_date        = models.DateField(null=True, blank=True)
    end_date          = models.DateField(null=True, blank=True)
    uploaded_by       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                          null=True, related_name='uploaded_tc')
    uploaded_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'TC | {self.account} | {self.channel} | {self.month}'


class TCRow(models.Model):
    """Individual row from a Transmission Certificate file.

    Stores every aired spot the channel reported. Reconciliation matches these
    against ScheduleRows (planned) and LMRBRows (3rd-party confirmation).

    Late-aired rule: a TCRow can only match a ScheduleRow if tcrow.date >= schedulerow.date.
    Extra spots: TCRows not matched to any ScheduleRow (is_schedule_matched=False).
    3rd-party confirmation: TCRow cross-referenced with LMRBRow within ±5 sec tolerance.
    """
    account         = models.ForeignKey(Account, on_delete=models.CASCADE)
    tc_report       = models.ForeignKey(TransmissionReport, on_delete=models.CASCADE, related_name='rows')
    channel         = models.CharField(max_length=200)
    date            = models.DateField()
    programme       = models.CharField(max_length=500, blank=True, default='')
    tc_theme        = models.CharField(max_length=500)
    duration        = models.IntegerField(null=True, blank=True)
    aired_time      = models.CharField(max_length=30)

    dedup_key       = models.CharField(max_length=64, unique=True)

    # Reconciliation results (populated by tc_engine.reconcile_tc)
    is_schedule_matched = models.BooleanField(default=False, db_index=True)
    matched_schedule    = models.ForeignKey(
        ScheduleRow, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='tc_matches',
    )
    is_lmrb_confirmed   = models.BooleanField(default=False, db_index=True)
    matched_lmrb        = models.ForeignKey(
        LMRBRow, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='tc_confirmations',
    )
    is_extra            = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['date', 'aired_time']
        indexes = [
            models.Index(fields=['account', 'channel', 'date']),
            models.Index(fields=['account', 'channel', 'is_schedule_matched']),
        ]

    def __str__(self):
        return f'{self.channel} | {self.tc_theme} | {self.date} | {self.aired_time}'

    @staticmethod
    def make_dedup_key(account_id, channel, date, aired_time, tc_theme, dur):
        raw = f'tc|{account_id}|{channel}|{date}|{aired_time}|{tc_theme}|{dur}'
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


class ManualMatch(models.Model):
    """A manually confirmed match linking an LMRBRow to a ScheduleRow and/or TCRow.

    Supports three match modes:
    - 'schedule_lmrb' : Schedule + LMRB  (original mode)
    - '3way'          : Schedule + TC + LMRB  (full three-way)
    - 'tc_lmrb'       : TC + LMRB only  (no corresponding schedule row)

    Workflow:
    1. Operations user opens /dashboard/manual/.
    2. Selects rows from the relevant panels, optionally adds a note, clicks "Match".
    3. Rows are locked (is_manual_matched=True) so the engine skips them.
    4. De-matching removes the record and unlocks all rows.
    """
    MATCH_MODES = [
        ('schedule_lmrb', 'Schedule + LMRB'),
        ('3way',          'Schedule + TC + LMRB'),
        ('tc_lmrb',       'TC + LMRB only'),
    ]

    account      = models.ForeignKey(Account, on_delete=models.CASCADE,
                                     related_name='manual_matches')
    channel      = models.CharField(max_length=200)
    month        = models.CharField(max_length=50)
    match_mode   = models.CharField(max_length=20, choices=MATCH_MODES,
                                    default='schedule_lmrb')
    schedule_row = models.OneToOneField(
        ScheduleRow, on_delete=models.CASCADE,
        related_name='manual_match',
        null=True, blank=True,
    )
    tc_row       = models.OneToOneField(
        'TCRow', on_delete=models.CASCADE,
        related_name='manual_match',
        null=True, blank=True,
    )
    lmrb_row     = models.OneToOneField(
        LMRBRow, on_delete=models.CASCADE,
        related_name='manual_match',
    )
    note         = models.TextField(blank=True, default='')
    matched_at   = models.DateTimeField(auto_now_add=True)
    matched_by   = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='manual_matches',
    )

    class Meta:
        ordering = ['-matched_at']

    def __str__(self):
        if self.schedule_row_id:
            return (
                f'ManualMatch | {self.account} | {self.channel} | '
                f'{self.schedule_row.brand} | {self.schedule_row.date}'
            )
        if self.tc_row_id:
            return f'ManualMatch | TC | {self.account} | {self.channel} | {self.tc_row.tc_theme}'
        return f'ManualMatch | {self.account} | {self.channel}'


class SponsorshipLmrbAssignment(models.Model):
    """Tracks the assignment of an LMRBRow to a SPONSORSHIP ScheduleRow.

    Created either automatically by sponsorship_engine.reconcile_sponsorship
    (match_type='auto') or manually by an operations user via the "Add from LMRB"
    picker in the Summary Sheet (match_type='manual').

    Once created, both the LMRBRow (is_sponsorship_matched=True) and the
    SponsorshipLmrbAssignment record act as a permanent lock — the LMRB row
    cannot be reused for any other schedule row.

    Constraints:
    - Each LMRBRow can have at most one assignment (unique lmrb_row).
    - Each SPONSORSHIP ScheduleRow can have at most one assignment (unique schedule_row).
    """
    MATCH_TYPE_CHOICES = [
        ('auto',   'Auto (Step 1)'),
        ('manual', 'Manual (Step 2)'),
    ]
    account      = models.ForeignKey(Account, on_delete=models.CASCADE,
                                     related_name='sponsorship_assignments')
    schedule_row = models.OneToOneField(
        ScheduleRow, on_delete=models.CASCADE,
        related_name='sponsorship_assignment',
    )
    lmrb_row     = models.OneToOneField(
        LMRBRow, on_delete=models.CASCADE,
        related_name='sponsorship_assignment',
    )
    match_type   = models.CharField(max_length=10, choices=MATCH_TYPE_CHOICES,
                                    default='auto')
    matched_at   = models.DateTimeField(auto_now_add=True)
    matched_by   = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='sponsorship_assignments',
    )

    class Meta:
        ordering = ['schedule_row__date', 'schedule_row__start_time']

    def __str__(self):
        return (
            f'Spon | {self.account} | {self.schedule_row.brand} '
            f'| {self.schedule_row.date} | {self.match_type}'
        )


class SummaryReportMeta(models.Model):
    """User-fillable metadata for the Summary Sheet report.
    One record per (account, channel, month) scope.
    """
    account              = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='summary_metas')
    channel              = models.CharField(max_length=200)
    month                = models.CharField(max_length=50)
    supplier_invoice_no  = models.CharField(max_length=500, blank=True, default='')
    po_no                = models.CharField(max_length=200, blank=True, default='')
    invoice_no           = models.CharField(max_length=200, blank=True, default='')
    notes                = models.TextField(blank=True, default='')
    prepared_by          = models.CharField(max_length=200, blank=True, default='')
    checked_by           = models.CharField(max_length=200, blank=True, default='')
    authorised_by        = models.CharField(max_length=200, blank=True, default='')
    created_at           = models.DateTimeField(auto_now_add=True)
    updated_at           = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['account', 'channel', 'month']
        ordering = ['-updated_at']

    def __str__(self):
        return f'Summary | {self.account} | {self.channel} | {self.month}'


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
        ('manual_match',       'Manually Matched'),
        ('extra_aired',        'Extra Aired'),
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


# ── System Settings ───────────────────────────────────────────────────────────

SETTING_DEFAULTS = [
    # ── Reconciliation ────────────────────────────────────────────────────────
    {
        'key': 'tc_lmrb_time_tolerance',
        'value': '5',
        'label': 'TC–LMRB Time Tolerance (seconds)',
        'description': (
            'Maximum time difference (±seconds) allowed between the TC aired time and the LMRB '
            'advt time for a cross-check match. Increase this value if TC and LMRB timestamps '
            'differ by more than 5 seconds (e.g. set to 30 if your previous system used 30 s). '
            'Default: 5.'
        ),
        'category': 'reconciliation',
    },
    # ── TC file column aliases ─────────────────────────────────────────────────
    {
        'key': 'tc_extra_theme_aliases',
        'value': '',
        'label': 'TC Theme — Extra Column Aliases',
        'description': (
            'Extra column names to try when detecting the ad-theme column in TC files. '
            'Comma-separated. '
            'Built-in: TC_Theme, Advt_Theme, Advt_theme, Theme, theme, Product, '
            'Description, Ad Name, AdName, Ad_Name'
        ),
        'category': 'tc_parsing',
    },
    {
        'key': 'tc_extra_time_aliases',
        'value': '',
        'label': 'TC Aired Time — Extra Column Aliases',
        'description': (
            'Extra column names to try for the aired/broadcast time column in TC files. '
            'Comma-separated. '
            'Built-in: Aired_Time, Advt_Time, Advt_time, advt_Time, Time, Aired Time, '
            'Ad Start, AdTime, AiredTime'
        ),
        'category': 'tc_parsing',
    },
    {
        'key': 'tc_extra_date_aliases',
        'value': '',
        'label': 'TC Date — Extra Column Aliases',
        'description': (
            'Extra column names to try for the broadcast date column in TC files. '
            'Comma-separated. '
            'Built-in: Date, Aired Date, Prg Date, aired_date, AiredDate, Prg_Date'
        ),
        'category': 'tc_parsing',
    },
    {
        'key': 'tc_extra_duration_aliases',
        'value': '',
        'label': 'TC Duration — Extra Column Aliases',
        'description': (
            'Extra column names to try for the ad-duration column in TC files. '
            'Comma-separated. '
            'Built-in: Duration, Dur, Seconds, Ad Dur, Duration_Sec'
        ),
        'category': 'tc_parsing',
    },
    {
        'key': 'tc_extra_programme_aliases',
        'value': '',
        'label': 'TC Programme — Extra Column Aliases',
        'description': (
            'Extra column names to try for the programme/show-name column in TC files. '
            'Comma-separated. '
            'Built-in: Programme, Program, Prg Name, PrgName, programme'
        ),
        'category': 'tc_parsing',
    },
    # ── LMRB file column aliases ───────────────────────────────────────────────
    {
        'key': 'lmrb_extra_theme_aliases',
        'value': '',
        'label': 'LMRB Theme — Extra Column Aliases',
        'description': (
            'Extra column names to try for the ad-theme column in LMRB/MapOnline files. '
            'Comma-separated. '
            'Built-in: Advt_Theme, Theme'
        ),
        'category': 'lmrb_parsing',
    },
    {
        'key': 'lmrb_extra_time_aliases',
        'value': '',
        'label': 'LMRB Advt Time — Extra Column Aliases',
        'description': (
            'Extra column names to try for the broadcast time column in LMRB/MapOnline files. '
            'Comma-separated. '
            'Built-in: Advt_time, Ad Start, Advt_Time'
        ),
        'category': 'lmrb_parsing',
    },
    {
        'key': 'lmrb_extra_duration_aliases',
        'value': '',
        'label': 'LMRB Duration — Extra Column Aliases',
        'description': (
            'Extra column names to try for the duration column in LMRB/MapOnline files. '
            'Comma-separated. '
            'Built-in: Dur, Ad Dur'
        ),
        'category': 'lmrb_parsing',
    },
    {
        'key': 'lmrb_extra_date_aliases',
        'value': '',
        'label': 'LMRB Date — Extra Column Aliases',
        'description': (
            'Extra column names to try for the date column in LMRB/MapOnline files. '
            'Comma-separated. '
            'Built-in: Date, Prg Date'
        ),
        'category': 'lmrb_parsing',
    },
    # ── LMRB sponsorship detection ─────────────────────────────────────────────
    {
        'key': 'lmrb_sponsorship_keywords',
        'value': '-BB,Com Break,DJ,-Extro,-Intro,-LLogo,Tag,Time Check,-Tr',
        'label': 'LMRB Sponsorship Theme Keywords',
        'description': (
            'Comma-separated keywords. If an LMRB Advt_Theme contains any of these '
            'as a case-insensitive substring, the row is classified as Sponsorship Benefits '
            'in analytics — regardless of BrandMapping. Add more keywords as needed. '
            'Default: -BB, Com Break, DJ, -Extro, -Intro, -LLogo, Tag, Time Check, -Tr'
        ),
        'category': 'lmrb_parsing',
    },
    # ── WhatsApp Business API ──────────────────────────────────────────────────
    {
        'key': 'whatsapp_enabled',
        'value': '0',
        'label': 'WhatsApp Notifications Enabled',
        'description': (
            'Set to 1 to enable WhatsApp notifications. '
            'Requires Access Token and Phone Number ID to be configured below. '
            'Set to 0 to disable all WhatsApp messages.'
        ),
        'category': 'whatsapp',
    },
    {
        'key': 'whatsapp_access_token',
        'value': '',
        'label': 'WhatsApp Access Token',
        'description': (
            'Meta WhatsApp Cloud API access token. '
            'Get this from developers.facebook.com → Your App → WhatsApp → API Setup. '
            'For testing use the temporary token; for production use a permanent token.'
        ),
        'category': 'whatsapp',
    },
    {
        'key': 'whatsapp_phone_number_id',
        'value': '',
        'label': 'WhatsApp Phone Number ID',
        'description': (
            'The Phone Number ID from Meta WhatsApp API Setup page. '
            'This is the ID of the number that sends messages (NOT the phone number itself).'
        ),
        'category': 'whatsapp',
    },
    {
        'key': 'whatsapp_test_number',
        'value': '',
        'label': 'Test WhatsApp Number',
        'description': (
            'During testing, ALL notifications go to this number instead of the real officer numbers. '
            'Include country code, e.g. +94771234567. '
            'Leave blank to send to real officer numbers (production mode).'
        ),
        'category': 'whatsapp',
    },
    {
        'key': 'whatsapp_app_base_url',
        'value': '',
        'label': 'App Base URL',
        'description': (
            'The public URL of this app, e.g. https://ad-monitor.railway.app. '
            'Used to generate TC upload links in WhatsApp messages. '
            'No trailing slash.'
        ),
        'category': 'whatsapp',
    },
    {
        'key': 'whatsapp_webhook_verify_token',
        'value': '',
        'label': 'Webhook Verify Token',
        'description': (
            'A secret token you choose (any random string) — set the same value in '
            'Meta Developer Console → WhatsApp → Configuration → Webhook Verify Token. '
            'Required to receive incoming WhatsApp messages for auto-registration. '
            'Example: ad-monitor-secret-2025'
        ),
        'category': 'whatsapp',
    },
    {
        'key': 'whatsapp_company_name',
        'value': 'Ogilvy Nova',
        'label': 'Company Name for Welcome Message',
        'description': (
            'Company name shown in the WhatsApp welcome message sent to new channel officers. '
            'e.g. "Welcome to Ogilvy Nova, [Name]!"'
        ),
        'category': 'whatsapp',
    },
]


class SystemSetting(models.Model):
    """
    Site-wide configuration editable by super_admin at /dashboard/settings/.

    Use the module-level helpers (get_setting / get_setting_int / get_setting_list)
    to read values from views, parsers, and engines — never query this model directly.
    Settings are auto-created with defaults when the settings page is first visited.
    """
    CATEGORY_CHOICES = [
        ('reconciliation', 'Reconciliation'),
        ('tc_parsing',     'TC File Parsing'),
        ('lmrb_parsing',   'LMRB / MapOnline File Parsing'),
        ('whatsapp',       'WhatsApp Notifications'),
    ]

    key         = models.CharField(max_length=100, unique=True)
    value       = models.TextField(blank=True, default='')
    label       = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category    = models.CharField(max_length=50, choices=CATEGORY_CHOICES,
                                   default='reconciliation')
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['category', 'key']

    def __str__(self):
        return self.key


# ── Setting helpers ───────────────────────────────────────────────────────────

def _ensure_defaults():
    """Create any missing SystemSetting rows from SETTING_DEFAULTS."""
    for d in SETTING_DEFAULTS:
        SystemSetting.objects.get_or_create(
            key=d['key'],
            defaults={
                'value':       d.get('value', ''),
                'label':       d['label'],
                'description': d.get('description', ''),
                'category':    d.get('category', 'reconciliation'),
            },
        )


def get_setting(key: str, default: str = '') -> str:
    """Return a SystemSetting value by key. Falls back to *default* if missing or blank."""
    try:
        val = SystemSetting.objects.get(key=key).value.strip()
        return val if val else default
    except SystemSetting.DoesNotExist:
        return default


def get_setting_int(key: str, default: int = 0) -> int:
    """Return a SystemSetting value parsed as int."""
    try:
        return int(get_setting(key, str(default)))
    except (ValueError, TypeError):
        return default


def get_setting_list(key: str) -> list:
    """Return a SystemSetting value as a list of stripped strings, split by comma."""
    val = get_setting(key, '')
    return [v.strip() for v in val.split(',') if v.strip()]


# ── Audit Log ─────────────────────────────────────────────────────────────────

class AuditLog(models.Model):
    """Records sensitive admin actions for accountability and debugging."""

    ACTION_CHOICES = [
        ('settings_change', 'Settings Changed'),
        ('db_reset',        'DB Reset'),
        ('db_delete',       'DB Delete'),
        ('db_backup',       'DB Backup'),
        ('db_dedup',        'Duplicate Removal'),
        ('user_create',     'User Created'),
        ('user_toggle',     'User Activated/Deactivated'),
        ('user_pwd_reset',  'Password Reset'),
        ('user_accounts',   'User Accounts Updated'),
    ]

    user      = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, related_name='audit_logs'
    )
    action    = models.CharField(max_length=50, choices=ACTION_CHOICES)
    detail    = models.TextField(blank=True, default='')
    ip        = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f'{self.user} — {self.action} — {self.timestamp:%Y-%m-%d %H:%M}'


# ── Channel Officer ────────────────────────────────────────────────────────────

class ChannelOfficer(models.Model):
    """Links a channel_officer User to a specific Account + Channel.

    When a channel officer logs in they are redirected to the TC upload page
    pre-filled with their account/channel. WhatsApp notifications for missed
    spots and TC upload reminders are sent to their whatsapp_number on the
    linked User record.
    """
    account        = models.ForeignKey('Account', on_delete=models.CASCADE,
                                       related_name='channel_officers')
    channel        = models.CharField(max_length=200)
    user           = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='channel_officer_profile',
        null=True, blank=True,
        help_text='The system login account for this officer',
    )
    name           = models.CharField(max_length=150)
    whatsapp_number = models.CharField(max_length=20, blank=True, default='',
                                       help_text='Include country code, e.g. +94771234567')
    is_whatsapp_registered = models.BooleanField(
        default=False,
        help_text='True once the officer has sent their first WhatsApp message to the business number.',
    )
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['account', 'channel', 'name']
        unique_together = [('account', 'channel', 'name')]

    def __str__(self):
        return f'{self.name} — {self.account} / {self.channel}'

    @property
    def effective_whatsapp(self):
        """Return officer's own number, or fall back to their linked user's number."""
        if self.whatsapp_number:
            return self.whatsapp_number
        if self.user and self.user.whatsapp_number:
            return self.user.whatsapp_number
        return ''
