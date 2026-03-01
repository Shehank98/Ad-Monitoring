from django.urls import path
from . import views

urlpatterns = [
    path('',          views.tool,              name='verification_tool'),
    path('channels/', views.get_channels,      name='verification_channels'),
    path('months/',   views.get_months,        name='verification_months'),
    path('preview/',  views.get_preview,       name='verification_preview'),
    path('run/',      views.run_verification,  name='run_verification'),
    path('export/',   views.export_excel,      name='export_excel'),
]
