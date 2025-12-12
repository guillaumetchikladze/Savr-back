from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

urlpatterns = [
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('profile/', views.profile_view, name='profile'),
    path('profile/upload-avatar/', views.upload_avatar_view, name='upload_avatar'),
    path('profile/confirm-avatar-upload/', views.confirm_avatar_upload_view, name='confirm_avatar_upload'),
    path('search/', views.search_view, name='search'),
    path('users/<int:user_id>/', views.user_detail_view, name='user_detail'),
    path('users/<int:user_id>/follow/', views.follow_user_view, name='follow_user'),
    path('notifications/', views.notifications_view, name='notifications'),
    path('notifications/unread-count/', views.unread_notifications_count_view, name='unread_notifications_count'),
    path('notifications/<int:notification_id>/read/', views.mark_notification_read_view, name='mark_notification_read'),
    path('notifications/read-all/', views.mark_all_notifications_read_view, name='mark_all_notifications_read'),
    path('complices/', views.complices_view, name='complices'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
]

