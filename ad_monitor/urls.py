from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect
from django.views.generic import TemplateView
from core.views import offline_page
import os

def _service_worker(request):
    """Serve the service worker from the root so its scope covers the whole site."""
    from django.http import HttpResponse
    sw_path = os.path.join(settings.BASE_DIR, 'static', 'js', 'sw.js')
    with open(sw_path, 'r') as f:
        content = f.read()
    return HttpResponse(content, content_type='application/javascript')

urlpatterns = [
    path('',                 lambda req: redirect('/dashboard/')),
    path('sw.js',            _service_worker,  name='service_worker'),
    path('offline/',         offline_page,     name='offline'),
    path('privacy-policy/',  TemplateView.as_view(template_name='privacy_policy.html'), name='privacy_policy'),
    path('django-admin/',    admin.site.urls),
    path('auth/',            include('accounts.urls')),
    path('dashboard/',       include('core.urls')),
    path('verify/',          include('verification.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
