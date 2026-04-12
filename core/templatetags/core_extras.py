from django import template

register = template.Library()


@register.filter
def split_pipe(value):
    """Split a pipe-separated string into a list of non-empty stripped items."""
    if not value:
        return []
    return [t.strip() for t in str(value).split('|') if t.strip()]
