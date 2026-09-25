from django.contrib import admin

from .models import AllowedSender, InboundAttachment, InboundEmail


@admin.register(AllowedSender)
class AllowedSenderAdmin(admin.ModelAdmin):
    list_display = ('email', 'name', 'account', 'is_active')


class AttachmentInline(admin.TabularInline):
    model = InboundAttachment
    extra = 0
    readonly_fields = ('filename', 'sha256', 'size', 'status', 'reason')


@admin.register(InboundEmail)
class InboundEmailAdmin(admin.ModelAdmin):
    list_display = ('sender', 'subject', 'received_at', 'status', 'reason')
    list_filter = ('status', 'reason')
    inlines = [AttachmentInline]
