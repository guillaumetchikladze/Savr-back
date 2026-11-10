"""
Carnet d'un profil (`recipes/services/carnet_service.py`) : plats cuisinés, avec ou sans recette, paginés.

- `GET /api/carnet/me/` : mon carnet (tout, y compris « rien que pour moi » et les repas sans post).
- `GET /api/carnet/users/<id>/` : le carnet d'un profil ; soi → comme `me/` ; un autre → ses posts publiés
  visibles seulement. Profil non visible (pas abonné, blocage) → 403 `profile_not_visible` ; inconnu ou
  inactif → 404.
- `?cursor=` (renvoyé par `next_cursor`), `?page_size=` (défaut 20, ≤ 50).

Réponse : `{count, next_cursor, results: [{key, kind, date, meal_time, meal_time_display, title, dish_key,
recipe {id, title, image_url} | null, post_id | null, thumbnail {url, width, height} | null, photos_count,
note, first_time, is_private, [soi : batch_id, meal_plan_id, servings]}]}`.
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated

from .services import carnet_service


def _page_size(request) -> int:
    try:
        return int(request.query_params.get('page_size') or carnet_service.DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        return carnet_service.DEFAULT_PAGE_SIZE


def _carnet(request, owner_id: int) -> Response:
    try:
        owner = carnet_service.get_owner_or_raise(request.user, owner_id)
    except carnet_service.CarnetNotVisible as exc:
        if str(exc) == 'not_found':
            return Response({'error': 'Utilisateur introuvable.', 'code': 'not_found'}, status=404)
        return Response({'error': 'Profil non visible.', 'code': 'profile_not_visible'}, status=403)
    try:
        data = carnet_service.build_carnet_page(
            request.user, owner, cursor=request.query_params.get('cursor') or None, page_size=_page_size(request)
        )
    except carnet_service.InvalidCursor:
        return Response({'error': 'Curseur invalide.', 'code': 'invalid_cursor'}, status=400)
    return Response({'user_id': owner.id, **data})


@api_view(['GET'])
@permission_classes([IsValidated])
def carnet_me_view(request):
    return _carnet(request, request.user.id)


@api_view(['GET'])
@permission_classes([IsValidated])
def carnet_user_view(request, user_id: int):
    return _carnet(request, user_id)
