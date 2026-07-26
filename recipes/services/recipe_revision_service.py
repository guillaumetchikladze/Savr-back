"""Révision de recette via l'agent (proposition + application confirmée)."""

import json
from decimal import Decimal

from asgiref.sync import async_to_sync
from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction

from chat.services.tool_schemas import MutationProposal
from recipes.models import Ingredient, Recipe, RecipeIngredient, Step
from recipes.services.ai_service import formalized_to_response_dict, revise_recipe
from recipes.services.recipe_search_index import schedule_recipe_search_reindex


def build_recipe_state_dict(recipe: Recipe) -> dict:
    ingredients = []
    for ri in RecipeIngredient.objects.filter(recipe=recipe).select_related('ingredient'):
        name = ri.ingredient.name if ri.ingredient_id else ''
        ingredients.append({
            'ingredient_name': name,
            'quantity': float(ri.quantity) if ri.quantity is not None else 0,
            'unit': ri.unit or 'g',
        })

    steps = []
    for step in Step.objects.filter(recipe=recipe).order_by('order'):
        steps.append({
            'title': step.title or '',
            'instruction': step.instruction or '',
            'tip': step.tip or '',
            'has_timer': bool(step.has_timer),
            'timer_duration': step.timer_duration,
        })

    return {
        'title': recipe.title or '',
        'description': recipe.description or '',
        'steps_summary': recipe.steps_summary or '',
        'meal_type': recipe.meal_type or 'dinner',
        'difficulty': recipe.difficulty or 'medium',
        'prep_time': recipe.prep_time,
        'cook_time': recipe.cook_time,
        'servings': recipe.servings,
        'ingredients': ingredients,
        'steps': steps,
    }


def _normalized_snapshot(state: dict) -> str:
    return json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)


def has_recipe_changes(before: dict, after: dict) -> bool:
    return _normalized_snapshot(before) != _normalized_snapshot(after)


def propose_recipe_revision_data(
    user: AbstractBaseUser,
    recipe_id: int,
    instruction: str,
    scope: str = 'full',
) -> MutationProposal:
    recipe = Recipe.objects.filter(id=recipe_id, created_by=user).first()
    if not recipe:
        raise ValueError('Recette introuvable ou non autorisée.')

    instruction = (instruction or '').strip()
    if not instruction:
        raise ValueError('Instruction de modification requise.')

    if scope not in ('full', 'ingredients', 'steps', 'meta'):
        scope = 'full'

    before_state = build_recipe_state_dict(recipe)
    revised = async_to_sync(revise_recipe)(before_state, instruction, scope)
    after_formalized = formalized_to_response_dict(revised)

    if not has_recipe_changes(before_state, after_formalized):
        raise ValueError('Tchikook n\'a proposé aucun changement pour cette recette.')

    return MutationProposal(
        card_type='recipe_revision',
        title=recipe.title,
        subtitle='Modifications proposées par Tchikook',
        details={
            'recipe_id': recipe.id,
            'recipe_title': recipe.title,
            'instruction': instruction,
            'scope': scope,
            'before_state': before_state,
            'after_formalized': after_formalized,
        },
    )


def _apply_edit_payload(recipe: Recipe, data: dict) -> None:
    meta_fields = [
        'title', 'description', 'steps_summary', 'meal_type', 'difficulty',
        'prep_time', 'cook_time', 'servings', 'image_path', 'is_public',
    ]
    for field in meta_fields:
        if field in data:
            setattr(recipe, field, data.get(field))
    recipe.save()

    if 'ingredients' in data:
        RecipeIngredient.objects.filter(recipe=recipe).delete()
        for item in data.get('ingredients') or []:
            if not isinstance(item, dict):
                continue
            ingredient_name = (
                item.get('ingredient_name')
                or (item.get('ingredient') or {}).get('name')
                or item.get('name')
                or ''
            ).strip()
            if not ingredient_name:
                continue
            ingredient_obj, _ = Ingredient.objects.get_or_create(name=ingredient_name)
            from recipes.services.ingredient_categorization import ensure_ingredient_category

            ensure_ingredient_category(ingredient_obj)
            try:
                quantity_decimal = Decimal(str(item.get('quantity', 0)))
            except Exception:
                quantity_decimal = Decimal('0')
            RecipeIngredient.objects.create(
                recipe=recipe,
                ingredient=ingredient_obj,
                quantity=quantity_decimal,
                unit=item.get('unit') or 'g',
            )

    if 'steps' in data:
        Step.objects.filter(recipe=recipe).delete()
        for idx, step_data in enumerate(data.get('steps') or []):
            if not isinstance(step_data, dict):
                continue
            Step.objects.create(
                recipe=recipe,
                order=idx,
                title=step_data.get('title', '') or '',
                instruction=step_data.get('instruction') or step_data.get('text') or '',
                tip=step_data.get('tip', '') or '',
                has_timer=bool(step_data.get('has_timer', False)),
                timer_duration=step_data.get('timer_duration'),
            )


def _formalized_to_edit_payload(formalized: dict) -> dict:
    ingredients = []
    for ing in formalized.get('recipe_ingredients') or formalized.get('ingredients') or []:
        if not isinstance(ing, dict):
            continue
        ingredients.append({
            'ingredient_name': ing.get('ingredient_name') or ing.get('name') or '',
            'quantity': ing.get('quantity', 0),
            'unit': ing.get('unit') or 'g',
        })

    steps = []
    for step in formalized.get('steps') or []:
        if not isinstance(step, dict):
            continue
        steps.append({
            'title': step.get('title', '') or '',
            'instruction': step.get('instruction') or '',
            'tip': step.get('tip', '') or '',
            'has_timer': bool(step.get('has_timer', False)),
            'timer_duration': step.get('timer_duration'),
        })

    return {
        'title': formalized.get('title'),
        'description': formalized.get('description'),
        'steps_summary': formalized.get('steps_summary'),
        'meal_type': formalized.get('meal_type'),
        'difficulty': formalized.get('difficulty'),
        'prep_time': formalized.get('prep_time'),
        'cook_time': formalized.get('cook_time'),
        'servings': formalized.get('servings'),
        'ingredients': ingredients,
        'steps': steps,
    }


def execute_recipe_revision(user: AbstractBaseUser, payload: dict) -> dict:
    recipe_id = payload.get('recipe_id')
    after_formalized = payload.get('after_formalized')
    if not recipe_id or not after_formalized:
        raise ValueError('Payload de révision incomplet.')

    recipe = Recipe.objects.filter(id=recipe_id, created_by=user).first()
    if not recipe:
        raise ValueError('Recette introuvable ou non autorisée.')

    edit_payload = _formalized_to_edit_payload(after_formalized)
    with transaction.atomic():
        _apply_edit_payload(recipe, edit_payload)

    schedule_recipe_search_reindex(recipe.id)
    return {
        'recipe_id': recipe.id,
        'recipe_title': recipe.title,
        'message': 'Recette mise à jour.',
    }
