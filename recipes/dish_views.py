"""
« Le carnet du plat » et « Classiques de tes amis » (contrat front : .claude-work/specs/dish-page.md).

- `GET /api/dishes/<dish_key>/` (`r-<recipe_id>` ou `l-<slug>`, cf. `dish_service.dish_key`), raccourcis
  `GET /api/dishes/recipe/<id>/` et `GET /api/dishes/label/<texte ou slug>/` ; `?page=` / `?page_size=` (≤ 30)
  paginent « Tes versions ». Plat inconnu ou recette privée jamais croisée → 404 `dish_not_found`.
- `GET /api/dishes/classics/?week=YYYY-MM-DD` : plats cuisinés par ≥ 3 amis (posts visibles) sur 30 jours.
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated

from .services import dish_service
from .services.basket_service import BasketError


def _not_found() -> Response:
    return Response({'error': 'Dish not found', 'code': 'dish_not_found'}, status=404)


def _int_param(request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name) or default)
    except (TypeError, ValueError):
        return default


def _page(request, ref: dish_service.DishRef) -> Response:
    try:
        data = dish_service.build_dish_page(
            request.user,
            ref,
            request=request,
            page=_int_param(request, 'page', 1),
            page_size=_int_param(request, 'page_size', dish_service.VERSIONS_PAGE_SIZE),
        )
    except dish_service.DishNotFound:
        return _not_found()
    return Response(data)


@api_view(['GET'])
@permission_classes([IsValidated])
def dish_detail_view(request, dish_key: str):
    try:
        ref = dish_service.parse_dish_key(dish_key)
    except dish_service.DishNotFound:
        return _not_found()
    return _page(request, ref)


@api_view(['GET'])
@permission_classes([IsValidated])
def dish_recipe_view(request, recipe_id: int):
    return _page(request, dish_service.DishRef(recipe_id=recipe_id))


@api_view(['GET'])
@permission_classes([IsValidated])
def dish_label_view(request, slug: str):
    try:
        ref = dish_service.label_ref(slug)
    except dish_service.DishNotFound:
        return _not_found()
    return _page(request, ref)


@api_view(['GET'])
@permission_classes([IsValidated])
def dish_classics_view(request):
    try:
        data = dish_service.friends_classics(request.user, week=request.query_params.get('week'), request=request)
    except BasketError as exc:
        return Response({'error': exc.message, 'code': exc.code}, status=exc.status_code)
    return Response(data)
