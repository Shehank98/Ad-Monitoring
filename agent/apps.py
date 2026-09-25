from django.apps import AppConfig


class AgentAppConfig(AppConfig):
    """Nova reconciliation agent (AGENT_BUILD_BRIEF.md). Named AgentAppConfig so it
    never collides with the brief's future AgentConfig model."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'agent'
    verbose_name = 'Nova agent'
