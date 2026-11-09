"""
Sous-recettes « simplifié / compliqué » (contrat front : .claude-work/specs/sub-recipes.md).

    PUT|PATCH /api/recipe-ingredients/{line_id}/sub-recipe/   {sub_recipe_id, default_mode?, sub_recipe_ratio?}
    DELETE    /api/recipe-ingredients/{line_id}/sub-recipe/   délier (la ligne redevient un ingrédient acheté)
    GET       /api/recipe-ingredients/{line_id}/sub-recipe-suggestions/   recettes au titre proche (sans LLM)
    GET|POST  /api/recipe-batches/{batch_id}/sub-recipe-modes/  {modes: {line_id: "buy" | "make"}, replace?}
    POST      /api/basket/{id}/sub-recipe-modes/                (BasketViewSet, même corps)

Lier : auteur de la recette seulement (recette invisible → 404, visible d'un autre → 403) ; sous-recette publique
ou à soi ; boucle → 400 `cycle`, plus de 3 niveaux → 400 `too_deep`.
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated

from .serializers import RecipeIngredientSerializer
from .services import sub_recipe_service as subs
from .services.sub_recipe_service import SubRecipeError


def _error(exc: SubRecipeError) -> Response:
    return Response({'error': exc.message, 'code': exc.code}, status=exc.status_code)


def _line_response(request, line) -> Response:
    line.refresh_from_db()
    return Response(
        {
            'line': RecipeIngredientSerializer(line, context={'request': request}).data,
            **subs.recipe_sub_recipes_payload(line.recipe, request.user),
        }
    )


@api_view(['PUT', 'PATCH', 'DELETE'])
@permission_classes([IsValidated])
def recipe_ingredient_sub_recipe_view(request, line_id: int):
    try:
        line = subs.get_owned_line(request.user, line_id)
        if request.method == 'DELETE':
            subs.unlink_sub_recipe(line)
        else:
            data = request.data if isinstance(request.data, dict) else {}
            subs.link_sub_recipe(
                request.user,
                line,
                sub_recipe_id=data.get('sub_recipe_id'),
                default_mode=data.get('default_mode'),
                sub_recipe_ratio=data.get('sub_recipe_ratio'),
            )
    except SubRecipeError as exc:
        return _error(exc)
    return _line_response(request, line)


@api_view(['GET'])
@permission_classes([IsValidated])
def recipe_ingredient_sub_recipe_suggestions_view(request, line_id: int):
    try:
        line = subs.get_owned_line(request.user, line_id)
    except SubRecipeError as exc:
        return _error(exc)
    return Response(
        {
            'line_id': line.id,
            'ingredient': {'id': line.ingredient_id, 'name': line.ingredient.name},
            'current_sub_recipe_id': line.sub_recipe_id,
            'suggestions': subs.suggest_sub_recipes(request.user, line),
        }
    )


def batch_modes_payload(batch, user) -> dict:
    return {
        'recipe_batch_id': batch.id,
        'recipe_id': batch.recipe_id,
        'sub_recipe_modes': dict(batch.sub_recipe_modes or {}),
        **subs.recipe_sub_recipes_payload(batch.recipe, user, modes=batch.sub_recipe_modes),
    }


def apply_modes(batch, data) -> dict:
    data = data if isinstance(data, dict) else {}
    replace = str(data.get('replace', '')).strip().lower() in ('1', 'true', 'yes')
    subs.set_batch_modes(batch, data.get('modes', {}), replace=replace)
    return data


@api_view(['GET', 'POST', 'PUT'])
@permission_classes([IsValidated])
def recipe_batch_sub_recipe_modes_view(request, batch_id: int):
    batch = subs.accessible_batches(request.user).filter(id=batch_id).select_related('recipe').first()
    if batch is None:
        return Response({'error': 'Préparation introuvable.', 'code': 'not_found'}, status=404)
    if request.method != 'GET':
        try:
            body = request.data if isinstance(request.data, dict) else {}
            data = {'modes': body.get('modes'), 'replace': request.method == 'PUT' or body.get('replace')}
            apply_modes(batch, data)
        except SubRecipeError as exc:
            return _error(exc)
    return Response(batch_modes_payload(batch, request.user))
