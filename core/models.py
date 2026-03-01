from django.db import models
from django.conf import settings


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
    """An ad schedule Excel file uploaded by a Planner."""
    account         = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='schedules')
    channel         = models.CharField(max_length=200)
    month           = models.CharField(max_length=50)
    schedule_number = models.CharField(max_length=50)
    file            = models.FileField(upload_to='schedules/')
    original_filename = models.CharField(max_length=255)
    row_count       = models.PositiveIntegerField(default=0)
    uploaded_by     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                        null=True, related_name='uploaded_schedules')
    uploaded_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.account} | {self.channel} | {self.month} | #{self.schedule_number}'


class MonitoringData(models.Model):
    """A MapOnline or MediaWatch data file uploaded by Operations."""
    DATA_TYPES = [
        ('maponline',  'MapOnline'),
        ('mediawatch', 'MediaWatch (LMRB)'),
    ]
    account         = models.ForeignKey(Account, on_delete=models.SET_NULL,
                                        null=True, blank=True, related_name='monitoring_data')
    data_type       = models.CharField(max_length=20, choices=DATA_TYPES)
    channel         = models.CharField(max_length=200)
    start_date      = models.DateField()
    end_date        = models.DateField()
    file            = models.FileField(upload_to='monitoring/')
    original_filename = models.CharField(max_length=255)
    row_count       = models.PositiveIntegerField(default=0)
    uploaded_by     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                        null=True, related_name='uploaded_monitoring')
    uploaded_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f'{self.get_data_type_display()} | {self.channel} | {self.start_date}–{self.end_date}'


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
