from django.contrib import admin
from .models import CompetitorUploadBatch, CompetitorAd


@admin.register(CompetitorUploadBatch)
class CompetitorUploadBatchAdmin(admin.ModelAdmin):
    list_display = ('batch_id', 'account', 'original_filename', 'start_date', 'end_date', 'row_count', 'uploaded_by', 'uploaded_at', 'is_locked')
    list_filter = ('account', 'is_locked', 'uploaded_at')
    search_fields = ('original_filename', 'account__name')
    readonly_fields = ('batch_id', 'uploaded_at')
    ordering = ('-uploaded_at',)


@admin.register(CompetitorAd)
class CompetitorAdAdmin(admin.ModelAdmin):
    list_display = ('advertiser', 'product', 'channel', 'date', 'advt_time', 'duration', 'cost', 'account')
    list_filter = ('account', 'channel', 'language', 'date')
    search_fields = ('advertiser', 'product', 'advt_theme', 'channel', 'program')
    readonly_fields = ('dedup_key', 'created_at')
    ordering = ('-date', 'advertiser')
    date_hierarchy = 'date'
