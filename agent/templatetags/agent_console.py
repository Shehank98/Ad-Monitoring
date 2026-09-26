"""{% agent_card %}: the Reconciliation Agent sidebar card (Phase 3.2). Read-only.
Used by templates/base.html through patch docs/agent/patches/0007_base_agent_card.diff."""
from django import template

from agent.console import card

register = template.Library()


@register.inclusion_tag('agent/_agent_card.html', takes_context=True)
def agent_card(context):
    request = context.get('request')
    user = getattr(request, 'user', None)
    data = card(user) if user is not None else None          # card() never raises; None = not shown
    return {'card': data, 'request': request, 'csrf_token': context.get('csrf_token')}
