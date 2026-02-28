from django.contrib import admin
from .models import Account, Schedule, MonitoringData

admin.site.register(Account)

@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display  = ('account', 'channel', 'month', 'schedule_number', 'uploaded_by', 'uploaded_at')
    list_filter   = ('account', 'channel')
    search_fields = ('channel', 'schedule_number')

@admin.register(MonitoringData)
class MonitoringAdmin(admin.ModelAdmin):
    list_display  = ('data_type', 'channel', 'start_date', 'end_date', 'uploaded_by', 'uploaded_at')
    list_filter   = ('data_type', 'channel')
