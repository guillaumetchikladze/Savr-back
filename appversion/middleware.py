"""
Blocage optionnel des versions trop anciennes sur toute l'API (HTTP 426 Upgrade Required).

Désactivé par défaut (`APP_VERSION_ENFORCEMENT=False`). Actif, il ne concerne que les requêtes
/api/ portant `X-App-Version` + `X-App-Platform` : les vieux clients (sans en-têtes) et le web passent.
"""

from django.conf import settings
from django.http import JsonResponse

from .services import STATUS_UPDATE_REQUIRED, evaluate

EXEMPT_PATH_PREFIXES = (
    '/api/app/version-check/',
    '/api/health/',
    '/api/auth/token/refresh/',
    '/api/legal/',
    '/legal/',
)


class AppVersionEnforcementMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        blocked = self._blocked_payload(request)
        if blocked is not None:
            return JsonResponse(blocked, status=426)
        return self.get_response(request)

    @staticmethod
    def _blocked_payload(request):
        if not getattr(settings, 'APP_VERSION_ENFORCEMENT', False):
            return None
        path = request.path
        if not path.startswith('/api/') or path.startswith(EXEMPT_PATH_PREFIXES) or request.method == 'OPTIONS':
            return None
        version = request.headers.get('X-App-Version')
        platform = request.headers.get('X-App-Platform')
        if not version or not platform:
            return None
        payload = evaluate(platform, version)
        return payload if payload['status'] == STATUS_UPDATE_REQUIRED else None
