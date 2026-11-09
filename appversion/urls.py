from django.urls import path

from .views import AppVersionCheckView

urlpatterns = [
    path('version-check/', AppVersionCheckView.as_view(), name='app_version_check'),
]
