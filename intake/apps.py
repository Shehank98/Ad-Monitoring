from django.apps import AppConfig


class IntakeConfig(AppConfig):
    """Email TC intake (AGENT_BUILD_BRIEF.md §11). Phase 1: models only."""
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'intake'
    verbose_name = 'TC email intake'
