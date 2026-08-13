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
    try:
        from core.models import get_setting
        nova_enabled = get_setting('nova_enabled', '1') != '0'
    except Exception:
        nova_enabled = True
    return {
        'sidebar_logo_url':    _branding_url('logo'),
        'branding_logo_url':   _branding_url('logo'),
        'branding_tartan_url': _branding_url('tartan'),
        'nova_enabled':        nova_enabled,
    }


def site_notifications(request):
    """Inject active site-wide announcement banners for signed-in users."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {'site_notifications': []}
    try:
        from core.models import SiteNotification
        notes = list(SiteNotification.objects.filter(is_active=True)[:5])
    except Exception:
        notes = []
    return {'site_notifications': notes}
