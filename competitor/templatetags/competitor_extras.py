from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Return dictionary[key], defaulting to 0."""
    return dictionary.get(key, 0)
