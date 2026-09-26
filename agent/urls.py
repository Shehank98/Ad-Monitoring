from django.urls import include, path

from . import views

app_name = 'agent'

urlpatterns = [
    path('',          views.overview,     name='overview'),
    path('scopes/',   views.scope_list,   name='scopes'),
    path('scope/',    views.scope_detail, name='scope'),
    path('queue/',    views.queue,        name='queue'),
    path('activity/', views.activity,     name='activity'),
    path('config/',   views.config,       name='config'),
    path('audit/<int:pk>/', views.audit_report, name='audit_report'),
    path('finding/<int:pk>/feedback/', views.finding_feedback, name='finding_feedback'),
    path('finding/<int:pk>/acknowledge/', views.v5_acknowledge, name='v5_acknowledge'),
    path('console/schedules/', views.console_schedules, name='console_schedules'),
    path('console/reports/', views.console_reports, name='console_reports'),
    path('console/theme-tester/', views.console_theme_tester, name='console_theme_tester'),
    path('console/pause/', views.console_pause, name='console_pause'),
    path('console/run/', views.console_run, name='console_run'),
    path('inbox/',    include('intake.urls')),              # TC Inbox (owner Q5)
]
