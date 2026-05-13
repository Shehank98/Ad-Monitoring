from django import template

register = template.Library()


@register.filter
def split_pipe(value):
    """Split a pipe-separated string into a list of non-empty stripped items."""
    if not value:
        return []
    return [t.strip() for t in str(value).split('|') if t.strip()]


@register.filter
def get_item(dictionary, key):
    """Return dictionary[key], or None if the key is absent.  Used in templates
    to look up a value by a dynamic variable: {{ my_dict|get_item:var }}."""
    if not isinstance(dictionary, dict):
        return None
    return dictionary.get(key)
