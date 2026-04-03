from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect
from django.views.generic import TemplateView

urlpatterns = [
    path('',                 lambda req: redirect('/dashboard/')),
    path('privacy-policy/',  TemplateView.as_view(template_name='privacy_policy.html'), name='privacy_policy'),
    path('django-admin/',    admin.site.urls),
    path('auth/',            include('accounts.urls')),
    path('dashboard/',       include('core.urls')),
    path('verify/',          include('verification.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
