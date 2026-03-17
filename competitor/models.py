import uuid
from django.db import models
from core.models import Account
from django.conf import settings


class CompetitorUploadBatch(models.Model):
    """
    One record per Excel file uploaded for competitor analysis.
    Each batch belongs to a client Account — competitors are analysed per client.
    """
    batch_id          = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True, editable=False)
    account           = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='competitor_batches')
    original_filename = models.CharField(max_length=500, blank=True, default='')
    start_date        = models.DateField(null=True, blank=True)
    end_date          = models.DateField(null=True, blank=True)
    row_count         = models.IntegerField(default=0)
    uploaded_by       = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='competitor_uploads',
    )
    uploaded_at       = models.DateTimeField(auto_now_add=True)
    is_locked         = models.BooleanField(
        default=False,
        help_text='Locked batches cannot be deleted — used to preserve historical baselines.',
    )

    class Meta:
        ordering = ['-uploaded_at']
        verbose_name = 'Competitor Upload Batch'
        verbose_name_plural = 'Competitor Upload Batches'

    def __str__(self):
        return (f'{self.account} | {self.start_date} – {self.end_date} '
                f'({self.row_count} rows)')


class CompetitorAd(models.Model):
    """
    Individual monitoring row for a competitor advertisement.
    Parsed from the LMRB-style Excel upload.
    Deduplication: sha256(account|advertiser|product|advt_theme|channel|program|advt_time|date|duration)[:32]
    """
    account          = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='competitor_ads')
    upload_batch     = models.ForeignKey(
        CompetitorUploadBatch, on_delete=models.CASCADE,
        related_name='ads', null=True, blank=True,
    )

    # Core ad identity
    product_group    = models.CharField(max_length=300, blank=True, default='')
    advertiser       = models.CharField(max_length=300, db_index=True)
    product          = models.CharField(max_length=300, blank=True, default='')
    advt_theme       = models.CharField(max_length=500, blank=True, default='')

    # Broadcast placement
    channel          = models.CharField(max_length=200, db_index=True)
    program          = models.CharField(max_length=300, blank=True, default='')
    date             = models.DateField(db_index=True)
    prog_time        = models.CharField(max_length=30, blank=True, default='')
    advt_time        = models.CharField(max_length=30, blank=True, default='')

    # Break metadata
    ad_position      = models.IntegerField(null=True, blank=True)
    total_ads        = models.IntegerField(null=True, blank=True)
    break_no         = models.IntegerField(null=True, blank=True)
    position_in_break = models.IntegerField(null=True, blank=True)
    ads_in_break     = models.IntegerField(null=True, blank=True)

    # Creative attributes
    language         = models.CharField(max_length=50, blank=True, default='')
    duration         = models.IntegerField(null=True, blank=True)

    # Commercial
    cost             = models.DecimalField(max_digits=16, decimal_places=2, null=True, blank=True)

    # Deduplication
    dedup_key        = models.CharField(max_length=64, blank=True, default='', db_index=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'advt_time']
        indexes = [
            models.Index(fields=['account', 'date']),
            models.Index(fields=['account', 'advertiser']),
            models.Index(fields=['account', 'channel']),
            models.Index(fields=['account', 'date', 'advertiser']),
        ]
        verbose_name = 'Competitor Ad'
        verbose_name_plural = 'Competitor Ads'

    def __str__(self):
        return f'{self.advertiser} | {self.channel} | {self.date} | {self.advt_time}'
