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
    path('inbox/',    include('intake.urls')),              # TC Inbox (owner Q5)
]
