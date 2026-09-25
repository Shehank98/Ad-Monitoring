from django.contrib import admin

from . import models as m


class ReadOnlyAdmin(admin.ModelAdmin):
    """Changes go through Agent Settings (gate.perform, logged); the Django admin only shows."""
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(m.AgentConfig)
class AgentConfigAdmin(ReadOnlyAdmin):
    list_display = ('enabled', 'autonomy_level', 'mapping_threshold', 'tc_intake_mode',
                    'upload_debounce_minutes', 'updated_at')


@admin.register(m.ScopeState)
class ScopeStateAdmin(admin.ModelAdmin):
    list_display = ('account', 'channel', 'month', 'state', 'reason', 'last_run_at')
    list_filter = ('state',)


@admin.register(m.AgentAction)
class AgentActionAdmin(admin.ModelAdmin):
    list_display = ('action_type', 'tier', 'scope', 'target_model', 'target_pk', 'created_at', 'reverted_at')
    readonly_fields = [f.name for f in m.AgentAction._meta.fields]


@admin.register(m.AgentProposal)
class AgentProposalAdmin(admin.ModelAdmin):
    list_display = ('kind', 'status', 'scope', 'confidence', 'created_at')
    list_filter = ('kind', 'status')


admin.site.register(m.AgentAccountOverride, ReadOnlyAdmin)
for model in (m.ScheduleStatus, m.AgentRun, m.SummarySnapshot,
              m.AgentAuthorisation, m.LlmCall, m.NotificationLog, m.ScopeLockRow, m.Heartbeat):
    admin.site.register(model)
