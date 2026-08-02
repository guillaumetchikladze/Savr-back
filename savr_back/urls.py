"""
URL configuration for savr_back project.
"""
from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import path, include


def health_view(request):
    return JsonResponse({"status": "ok"})


def legal_index_view(request):
    """Index des mentions légales."""
    return render(request, 'legal/index.html')


def legal_privacy_view(request):
    """Politique de confidentialité (requise pour le Play Store)."""
    return render(request, 'legal/privacy_policy.html')


def legal_terms_view(request):
    """Conditions générales d'utilisation / EULA."""
    return render(request, 'legal/terms.html')


def legal_delete_account_view(request):
    """Page de demande de suppression de compte."""
    return render(request, 'legal/delete_account.html')


urlpatterns = [
    path('legal/', legal_index_view, name='legal_index'),
    path('legal/privacy-policy/', legal_privacy_view, name='legal_privacy'),
    path('legal/terms/', legal_terms_view, name='legal_terms'),
    path('legal/delete-account/', legal_delete_account_view, name='legal_delete_account'),
    path('privacy-policy/', lambda r: redirect('legal_privacy', permanent=True), name='privacy_policy_redirect'),
    path('admin/', admin.site.urls),
    path('auth/password-reset/<str:uidb64>/<str:token>/', __import__('accounts.views').views.password_reset_confirm_view, name='password_reset_confirm'),
    path('api/health/', health_view),
    path('api/auth/', include('accounts.urls')),
    path('api/', include('recipes.urls')),
    path('api/chat/', include('chat.urls')),
]

# Daphne / ASGI ne sert pas les statics : en DEBUG, exposer ceux des apps (admin CSS…).
if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()

