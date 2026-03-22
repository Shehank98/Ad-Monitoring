from django.urls import path
from . import views

urlpatterns = [
    path('login/',                    views.login_view,           name='login'),
    path('logout/',                   views.logout_view,           name='logout'),
    path('change-password/',          views.change_password_view, name='change_password'),
    path('users/<int:user_id>/delete/', views.delete_user,        name='delete_user'),
]
