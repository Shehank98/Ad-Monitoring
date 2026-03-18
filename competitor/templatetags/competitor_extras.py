from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Return dictionary[key], defaulting to 0."""
    return dictionary.get(key, 0)


@register.filter
def fmt_m(value):
    """Format a large number as '1.2M', '45K', or '1,234'."""
    try:
        v = float(value)
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v / 1_000:.0f}K"
        return f"{round(v):,}"
    except (TypeError, ValueError):
        return "—"
