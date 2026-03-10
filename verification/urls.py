from django.urls import path
from . import views

urlpatterns = [
    path('',            views.tool,                 name='verification_tool'),
    path('channels/',   views.get_channels,          name='verification_channels'),
    path('months/',     views.get_months,            name='verification_months'),
    path('preview/',    views.get_preview,           name='verification_preview'),
    path('previous/',   views.get_previous_results,  name='verification_previous'),
    path('run/',        views.run_verification,      name='run_verification'),
    path('verify-row/',   views.verify_row,            name='verify_row'),
    path('load-results/', views.load_results,          name='load_results'),
    path('export/',        views.export_excel,  name='export_excel'),
    path('export-missed/', views.export_missed, name='export_missed'),
    path('export-all/',    views.export_all,    name='export_all'),
    path('report/',     views.report,                name='verification_report'),
]
