from django.urls import path
from . import views
from accounts import views as acc_views

urlpatterns = [
    # Dashboard
    path('',                    views.dashboard,         name='dashboard'),

    # User management (admin+)
    path('users/',              acc_views.user_list,     name='user_list'),
    path('users/create/',       acc_views.create_user,   name='create_user'),
    path('users/<int:user_id>/edit/', acc_views.edit_user, name='edit_user'),

    # Account management
    path('accounts/',           views.account_list,      name='account_list'),

    # Channel management
    path('channels/',           views.channel_list,      name='channel_list'),

    # Schedules
    path('schedules/',          views.schedule_list,     name='schedule_list'),
    path('schedules/upload/',   views.schedule_upload,   name='schedule_upload'),
    path('schedules/<int:pk>/delete/', views.schedule_delete, name='schedule_delete'),

    # Monitoring data
    path('monitoring/',         views.monitoring_list,   name='monitoring_list'),
    path('monitoring/upload/',  views.monitoring_upload, name='monitoring_upload'),
    path('monitoring/<int:pk>/delete/', views.monitoring_delete, name='monitoring_delete'),
]
