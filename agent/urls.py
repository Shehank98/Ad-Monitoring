from django.urls import path

from . import views

app_name = 'agent'

urlpatterns = [
    path('',          views.overview,     name='overview'),
    path('scopes/',   views.scope_list,   name='scopes'),
    path('scope/',    views.scope_detail, name='scope'),
    path('queue/',    views.queue,        name='queue'),
    path('activity/', views.activity,     name='activity'),
    path('config/',   views.config,       name='config'),
]
