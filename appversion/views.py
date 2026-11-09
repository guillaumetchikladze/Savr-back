from django.utils.cache import patch_cache_control
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import evaluate

CACHE_MAX_AGE_SECONDS = 300


class AppVersionCheckView(APIView):
    """GET /api/app/version-check/?platform=ios|android&version=1.0.9&build=42

    Public (aucune authentification : un JWT expiré/invalide ne doit pas provoquer de 401),
    toujours 200 : les entrées inconnues ou illisibles renvoient « ok » (fail-open).
    `build` est accepté pour information mais n'entre pas dans la décision.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = []

    def get(self, request):
        payload = evaluate(request.query_params.get('platform'), request.query_params.get('version'))
        response = Response(payload)
        patch_cache_control(response, public=True, max_age=CACHE_MAX_AGE_SECONDS)
        return response
