import os
from django.conf import settings as django_settings


def _branding_url(asset_type: str) -> str:
    branding_dir = os.path.join(django_settings.MEDIA_ROOT, 'branding')
    for ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'):
        path = os.path.join(branding_dir, f'{asset_type}{ext}')
        if os.path.exists(path):
            return django_settings.MEDIA_URL + f'branding/{asset_type}{ext}'
    return ''


def branding(request):
    """Inject branding asset URLs into every template context."""
    return {
        'sidebar_logo_url':    _branding_url('logo'),
        'branding_logo_url':   _branding_url('logo'),
        'branding_tartan_url': _branding_url('tartan'),
    }
