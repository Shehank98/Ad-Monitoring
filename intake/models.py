"""
Email TC intake models (AGENT_BUILD_BRIEF.md §11, Amendment A6).

Idempotent by message_id and attachment sha256. Email text, attachment content and
file names are data: they are never followed as instructions and never used as
channel, month or schedule values (those always come from a Schedule record).
"""
from django.db import models

REASONS = [
    ('', '—'), ('unknown_sender', 'Unknown sender'), ('link_only', 'Link only'),
    ('no_tc_attachment', 'No TC attachment'), ('columns_unrecognised', 'Columns unrecognised'),
    ('pdf_disagreement', 'PDF readings disagree'), ('shared_tc', 'Shared TC'),
    ('no_schedule', 'No schedule'), ('multiple_schedules', 'Multiple schedules'),
    ('conflict', 'Conflict'), ('schedule_frozen', 'Schedule frozen'),
    ('tc_already_exists', 'TC already exists'), ('low_brand_overlap', 'Low brand overlap'),
    ('date_out_of_range', 'Date out of range'), ('row_mismatch', 'Row mismatch'),
    ('suspicious_instruction', 'Suspicious instruction'), ('tool_error', 'Tool error'),
]
STATUS = [('new', 'New'), ('processing', 'Processing'), ('needs_review', 'Needs review'),
          ('suggested', 'Suggested'), ('uploaded', 'Uploaded'), ('ignored', 'Ignored')]


class AllowedSender(models.Model):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True, default='')
    account = models.ForeignKey('core.Account', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='+')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email


class InboundEmail(models.Model):
    message_id = models.CharField(max_length=255, unique=True)
    sender = models.EmailField()
    subject = models.CharField(max_length=500, blank=True, default='')
    received_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    body_text = models.TextField(blank=True, default='')
    status = models.CharField(max_length=14, choices=STATUS, default='new')
    reason = models.CharField(max_length=24, choices=REASONS, blank=True, default='')

    def __str__(self):
        return f'{self.sender}: {self.subject[:40]}'


class InboundAttachment(models.Model):
    email = models.ForeignKey(InboundEmail, on_delete=models.CASCADE, related_name='attachments')
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True, default='')
    size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True)
    file = models.FileField(upload_to='intake/', blank=True)
    status = models.CharField(max_length=14, choices=STATUS, default='new')
    reason = models.CharField(max_length=24, choices=REASONS, blank=True, default='')
    suggested_schedule = models.ForeignKey('core.Schedule', null=True, blank=True,
                                           on_delete=models.SET_NULL, related_name='+')
    tc_report = models.ForeignKey('core.TransmissionReport', null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['email', 'sha256'],
                                               name='intake_attachment_unique')]

    def __str__(self):
        return self.filename
