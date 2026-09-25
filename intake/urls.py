from django.urls import path

from . import views

urlpatterns = [
    path('',                  views.inbox,        name='inbox'),
    path('<int:pk>/',         views.inbox_detail, name='inbox_detail'),
    path('<int:pk>/decide/',  views.inbox_decide, name='inbox_decide'),
]
