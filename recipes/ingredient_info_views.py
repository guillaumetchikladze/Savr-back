"""
Fiche produit d'un ingrédient (« Courses » : toucher un article). Contrat front : `ITEM-20261110-0010-shp2.md`.

    GET /api/ingredients/<id>/info/              fiche en base, sinon générée (LLM, ≤ ~8 s) puis stockée pour tous
    GET /api/shopping-list-items/<id>/info/      idem pour l'ingrédient d'une ligne de courses (membre de la liste)

200 → fiche (`cached` = déjà en base) ; 404 ingrédient / ligne inconnu(e) ; 429 `rate_limited` ;
503 `info_unavailable` (génération impossible, rien n'est stocké, `Retry-After`).
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated
from accounts.services.invite_service import rate_limited

from .models import Ingredient, ShoppingListItem
from .services import ingredient_info as service

HOUR = 3600
RETRY_AFTER_SECONDS = 60


def _too_many():
    return Response(
        {'code': 'rate_limited', 'detail': 'Trop de fiches demandées, réessaie un peu plus tard.'},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


def _info_response(request, ingredient: Ingredient) -> Response:
    user_id = request.user.id
    read_limit = int(getattr(settings, 'INGREDIENT_INFO_READS_PER_USER_HOUR', 300))
    if rate_limited('ingredient_info:read', user_id, limit=read_limit, window_seconds=HOUR):
        return _too_many()

    info = service.get_cached(ingredient.id)
    if info:
        return Response(service.payload(info, ingredient, cached=True))

    gen_limit = int(getattr(settings, 'INGREDIENT_INFO_GENERATIONS_PER_USER_HOUR', 30))
    if rate_limited('ingredient_info:gen', user_id, limit=gen_limit, window_seconds=HOUR):
        return _too_many()
    try:
        info, cached = service.get_or_generate(ingredient)
    except service.InfoUnavailable:
        response = Response(
            {
                'code': 'info_unavailable',
                'detail': "La fiche de ce produit n'est pas disponible pour le moment. Réessaie plus tard.",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response['Retry-After'] = str(RETRY_AFTER_SECONDS)
        return response
    return Response(service.payload(info, ingredient, cached=cached))


@api_view(['GET'])
@permission_classes([IsValidated])
def ingredient_info_view(request, ingredient_id: int):
    ingredient = Ingredient.objects.select_related('category').filter(pk=ingredient_id).first()
    if ingredient is None:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    return _info_response(request, ingredient)


@api_view(['GET'])
@permission_classes([IsValidated])
def shopping_list_item_info_view(request, item_id: int):
    item = (
        ShoppingListItem.objects.select_related('ingredient__category')
        .filter(pk=item_id, shopping_list__members__user=request.user)
        .first()
    )
    if item is None:
        return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
    return _info_response(request, item.ingredient)
