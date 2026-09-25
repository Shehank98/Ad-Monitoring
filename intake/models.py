"""
Email TC intake models (AGENT_BUILD_BRIEF.md §11, Amendment A6, Phase 2 plan).

Idempotent by message_id and attachment sha256. Email text, attachment content and
file names are data: they are never followed as instructions and never used as
channel, month or schedule values (those always come from a Schedule record).

Attachment bytes live in the database (InboundAttachment.content) because Railway
services do not share a filesystem. Only the web process writes a TransmissionReport
file, when an admin clicks Confirm (intake/confirm.py).
"""
from django.db import models

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
ACCEPTED_EXTENSIONS = ('xlsx', 'xls', 'pdf')      # what the core TC path accepts (C8)

REASONS = [
    ('', '—'), ('unknown_sender', 'Unknown sender'), ('link_only', 'Link only'),
    ('no_tc_attachment', 'No TC attachment'), ('columns_unrecognised', 'Columns unrecognised'),
    ('pdf_disagreement', 'PDF readings disagree'), ('shared_tc', 'Shared TC'),
    ('no_schedule', 'No schedule'), ('multiple_schedules', 'Multiple schedules'),
    ('conflict', 'Conflict'), ('schedule_frozen', 'Schedule authorised'),
    ('tc_already_exists', 'TC already exists'), ('low_brand_overlap', 'Low brand overlap'),
    ('date_out_of_range', 'Date out of range'), ('row_mismatch', 'Row mismatch'),
    ('suspicious_instruction', 'Suspicious instruction'), ('tool_error', 'Tool error'),
    # Phase 2
    ('duplicate_active_number', 'Duplicate active schedule number'),
    ('schedule_locked', 'Schedule locked'), ('too_large', 'Attachment too large'),
    ('foreign_brands', "Other clients' brands"), ('llm_disagrees', 'Assistant disagrees with rules'),
    ('duplicate_attachment', 'Duplicate attachment'), ('unsupported_type', 'Unsupported file type'),
]
STATUS = [('new', 'New'), ('processing', 'Processing'), ('needs_review', 'Needs review'),
          ('suggested', 'Proposed'), ('uploaded', 'Uploaded'), ('ignored', 'Ignored'),
          ('duplicate', 'Duplicate'), ('rejected', 'Rejected')]


class AllowedSender(models.Model):
    """A channel traffic desk allowed to send TCs (owner Q2). `email_or_domain` is an
    exact address, or an exact domain (the part after "@"). `accounts` empty = all.
    `channel_hint` only narrows the schedule search; it is never stored on a core record."""
    email_or_domain = models.CharField(max_length=254, unique=True)
    channel_hint = models.CharField(max_length=200, blank=True, default='')
    accounts = models.ManyToManyField('core.Account', blank=True, related_name='+')
    active = models.BooleanField(default=True)
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['email_or_domain']

    def __str__(self):
        return self.email_or_domain

    def save(self, *args, **kwargs):
        self.email_or_domain = self.email_or_domain.strip().lower()
        super().save(*args, **kwargs)

    def matches(self, sender: str) -> bool:
        s = (sender or '').strip().lower()
        key = self.email_or_domain
        if '@' in key:
            return s == key
        return s.rpartition('@')[2] == key

    @classmethod
    def for_sender(cls, sender: str):
        """Active entries matching the sender: exact address first, then domain."""
        hits = [a for a in cls.objects.filter(active=True) if a.matches(sender)]
        return sorted(hits, key=lambda a: '@' not in a.email_or_domain)


class InboundEmail(models.Model):
    message_id = models.CharField(max_length=255, unique=True)
    imap_uid = models.CharField(max_length=40, blank=True, default='')
    sender = models.EmailField()
    subject = models.CharField(max_length=500, blank=True, default='')
    received_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    body_text = models.TextField(blank=True, default='')
    status = models.CharField(max_length=14, choices=STATUS, default='new')
    reason = models.CharField(max_length=24, choices=REASONS, blank=True, default='')
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-received_at', '-id']

    def __str__(self):
        return f'{self.sender}: {self.subject[:40]}'


class InboundAttachment(models.Model):
    email = models.ForeignKey(InboundEmail, on_delete=models.CASCADE, related_name='attachments')
    filename = models.CharField(max_length=255)
    ext = models.CharField(max_length=10, blank=True, default='')
    content_type = models.CharField(max_length=100, blank=True, default='')
    size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    content = models.BinaryField(blank=True, default=b'', editable=False)
    duplicate_of = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='duplicates')
    status = models.CharField(max_length=14, choices=STATUS, default='new')
    reason = models.CharField(max_length=24, choices=REASONS, blank=True, default='')
    note = models.CharField(max_length=300, blank=True, default='')
    suggested_schedule = models.ForeignKey('core.Schedule', null=True, blank=True,
                                           on_delete=models.SET_NULL, related_name='+')
    llm_hint_schedule = models.ForeignKey('core.Schedule', null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name='+')
    rule_verdict = models.JSONField(default=dict, blank=True)
    llm_verdict = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    tc_report = models.ForeignKey('core.TransmissionReport', null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='+')
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-id']
        constraints = [models.UniqueConstraint(fields=['email', 'sha256'],
                                               name='intake_attachment_unique')]

    def __str__(self):
        return self.filename
