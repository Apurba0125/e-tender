from django.contrib.auth import views as auth_views
from django.urls import path
from . import views

urlpatterns = [
    path('vendor/login/', views.vendor_login, name='vendor_login'),
    path('internal/login/', views.internal_login, name='internal_login'),
    path('logout/', views.do_logout, name='logout'),
    path('vendor/register/', views.register, name='register'),
    path('verify/<str:token>/', views.verify_email, name='verify_email'),
    path('vendors/', views.vendor_list, name='vendor_list'),
    path('vendors/<int:pk>/action/', views.vendor_action, name='vendor_action'),
    path('profile/', views.profile, name='profile'),
    path('password-reset/', auth_views.PasswordResetView.as_view(), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(), name='password_reset_complete'),
]
