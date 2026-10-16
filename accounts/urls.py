from django.urls import path
from .views import (
    register_view,
    login_view,
    logout_view,
    profile_view,
    edit_profile_view,
    forgot_password_view,
    reset_password_confirm_view,
    confirm_email_view,
    telegram_link_view,
    telegram_unlink_view,
    update_profile,
    set_language_view,
    profile_location_view,
    google_login_view,
    google_callback_view,
    google_signup_view,
)

urlpatterns = [
    path('login/', login_view, name='login'),
    path('register/', register_view, name='register'),
    path('logout/', logout_view, name='logout'),
    path('auth/google/', google_login_view, name='google_login'),
    path('auth/google/callback/', google_callback_view, name='google_callback'),
    path('auth/google/signup/', google_signup_view, name='google_signup'),

    path('profile/', profile_view, name='profile'),
    path('profile/edit/', edit_profile_view, name='edit_profile'),
    path('profile/telegram/', telegram_link_view, name='telegram_link'),
    path('profile/telegram/unlink/', telegram_unlink_view, name='telegram_unlink'),
    path('profile/language/', set_language_view, name='profile_language'),
    path('profile/location/', profile_location_view, name='profile_location'),

    path('forgot-password/', forgot_password_view, name='forgot_password'),
    path('reset-password/<str:token>/', reset_password_confirm_view, name='reset_password'),
    path('confirm-email/<str:token>/', confirm_email_view, name='confirm_email'),

    path('update-profile/', update_profile, name='update_profile'),
]