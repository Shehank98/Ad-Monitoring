from django.urls import path
from . import views

urlpatterns = [
    path('',            views.tool,              name='verification_tool'),
    path('columns/',    views.get_columns,        name='get_columns'),
    path('themes/',     views.get_themes,         name='get_themes'),
    path('run/',        views.run_verification,   name='run_verification'),
]
