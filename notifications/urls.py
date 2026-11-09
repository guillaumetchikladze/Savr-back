from django.urls import path

from .views import opened_view, preferences_view

urlpatterns = [
    path('preferences/', preferences_view, name='notification_preferences'),
    path('opened/', opened_view, name='notification_opened'),
]
