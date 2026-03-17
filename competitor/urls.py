from django.urls import path
from . import views

urlpatterns = [
    path('',                                    views.competitor_analysis,    name='competitor_analysis'),
    path('detect/',                             views.competitor_detect,      name='competitor_detect'),
    path('upload/',                             views.competitor_upload,      name='competitor_upload'),
    path('batch/<uuid:batch_id>/delete/',       views.competitor_delete_batch, name='competitor_delete_batch'),
    path('batch/<uuid:batch_id>/lock/',         views.competitor_toggle_lock,  name='competitor_toggle_lock'),
    path('export/',                             views.competitor_export,       name='competitor_export'),
]
