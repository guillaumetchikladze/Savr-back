"""
Fiche produit d'un ingrédient (« Courses » : toucher un article). Contrat front : `ITEM-20261110-0010-shp2.md`.

    GET /api/ingredients/<id>/info/              fiche en base, sinon générée (LLM, ≤ ~8 s) puis stockée pour tous
    GET /api/shopping-list-items/<id>/info/      idem pour l'ingrédient d'une ligne de courses (membre de la liste)

200 → fiche (`cached` = déjà en base) ; 404 `not_found` ingrédient / ligne inconnu(e) ; 429 `rate_limited` ;
503 `info_unavailable` (génération impossible, rien n'est stocké, `Retry-After`). Aucune exception ne sort de la
vue : toute erreur inattendue (base, cache, fournisseur…) est journalisée puis rendue en 503 `info_unavailable`.
Le `code` des 404 permet à l'app de distinguer « fiche introuvable » d'un serveur qui ne connaît pas encore la route
(404 HTML de Django, sans `code`).
"""

from __future__ import annotations

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated
from accounts.services.invite_service import rate_limited

from .models import Ingredient, ShoppingListItem
from .services import ingredient_info as service

logger = logging.getLogger(__name__)

HOUR = 3600
RETRY_AFTER_SECONDS = 60


def _too_many():
    return Response(
        {'code': 'rate_limited', 'detail': 'Trop de fiches demandées, réessaie un peu plus tard.'},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


def _not_found():
    return Response({'code': 'not_found', 'detail': 'Produit introuvable.'}, status=status.HTTP_404_NOT_FOUND)


def _unavailable():
    response = Response(
        {
            'code': 'info_unavailable',
            'detail': "La fiche de ce produit n'est pas disponible pour le moment. Réessaie plus tard.",
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
    response['Retry-After'] = str(RETRY_AFTER_SECONDS)
    return response


def _serve(request, ingredient: Ingredient) -> Response:
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
        return _unavailable()
    return Response(service.payload(info, ingredient, cached=cached))


def _safe(load, request, ref) -> Response:
    """`load()` → ingrédient (None = 404). Jamais de 500 : toute erreur inattendue → log + 503."""
    try:
        ingredient = load()
        if ingredient is None:
            return _not_found()
        return _serve(request, ingredient)
    except Exception:  # noqa: BLE001 — l'app affiche alors « Pas de fiche pour le moment »
        logger.exception('[ingredient_info] erreur inattendue (%s)', ref)
        return _unavailable()


@api_view(['GET'])
@permission_classes([IsValidated])
def ingredient_info_view(request, ingredient_id: int):
    def load():
        return Ingredient.objects.select_related('category').filter(pk=ingredient_id).first()

    return _safe(load, request, f'ingredient={ingredient_id}')


@api_view(['GET'])
@permission_classes([IsValidated])
def shopping_list_item_info_view(request, item_id: int):
    def load():
        item = (
            ShoppingListItem.objects.select_related('ingredient__category')
            .filter(pk=item_id, shopping_list__members__user=request.user)
            .first()
        )
        return item.ingredient if item is not None else None

    return _safe(load, request, f'item={item_id}')
