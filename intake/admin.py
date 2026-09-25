from django.contrib import admin

from .models import AllowedSender, InboundAttachment, InboundEmail


@admin.register(AllowedSender)
class AllowedSenderAdmin(admin.ModelAdmin):
    list_display = ('email_or_domain', 'channel_hint', 'active')
    filter_horizontal = ('accounts',)


class AttachmentInline(admin.TabularInline):
    model = InboundAttachment
    fk_name = 'email'
    extra = 0
    fields = ('filename', 'sha256', 'size', 'status', 'reason', 'suggested_schedule')
    readonly_fields = fields


@admin.register(InboundEmail)
class InboundEmailAdmin(admin.ModelAdmin):
    list_display = ('sender', 'subject', 'received_at', 'status', 'reason')
    list_filter = ('status', 'reason')
    inlines = [AttachmentInline]
