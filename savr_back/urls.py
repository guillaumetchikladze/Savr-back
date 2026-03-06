"""
URL configuration for savr_back project.
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path, include


def health_view(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health/', health_view),
    path('api/auth/', include('accounts.urls')),
    path('api/', include('recipes.urls')),
]

