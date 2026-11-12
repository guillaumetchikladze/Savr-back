import logging
from typing import Any

from django.db import transaction

from ..models import (
    Recipe,
    RecipeIngredient,
    Step,
    StepIngredient,
)
from .ai_service import verify_quantity_consistency
from .equipment_service import deferred_equipment_refresh, set_ai_hints
from .ingredient_categorization import ensure_ingredient_category
from .ingredient_matcher import get_or_create_ingredient
from .recipe_search_index import schedule_recipe_search_reindex

logger = logging.getLogger(__name__)

# Recipe.image_path is a relative S3 key (CharField max_length=500), not an external URL.
RECIPE_IMAGE_PATH_MAX_LENGTH = 500


def persistable_recipe_image_path(path: Any) -> str:
    """Keep only values that fit Recipe.image_path (relative S3 keys)."""
    if not isinstance(path, str):
        return ''
    value = path.strip()
    if not value:
        return ''
    if value.startswith('http://') or value.startswith('https://'):
        logger.info(
            '[FormalizationPipeline] Ignoring external image URL at create time (len=%d)',
            len(value),
        )
        return ''
    if len(value) > RECIPE_IMAGE_PATH_MAX_LENGTH:
        logger.warning(
            '[FormalizationPipeline] Truncating image_path from %d to %d chars',
            len(value),
            RECIPE_IMAGE_PATH_MAX_LENGTH,
        )
        return value[:RECIPE_IMAGE_PATH_MAX_LENGTH]
    return value


def create_recipe_from_formalized(formalized_recipe, data: dict[str, Any], user, *, target=None, keep_title=False):
    """
    Reprend l'ancienne logique synchronisée pour créer la recette
    à partir du résultat formalisé.

    `target` : recette existante à remplir au lieu d'en créer une (recette posée tout de suite par
    « J'ai cuisiné », ckd2 lot 2) ; ses ingrédients et étapes sont remplacés, son titre gardé si
    `keep_title` (nom donné par l'utilisateur), sa visibilité et son auteur inchangés.
    """
    inconsistencies = verify_quantity_consistency(formalized_recipe)
    if inconsistencies:
        logger.warning(f'Incohérences de quantités détectées: {inconsistencies}')

    with transaction.atomic(), deferred_equipment_refresh():
        # Collecter tous les noms uniques
        all_ingredient_names = set()
        for recipe_ingredient in formalized_recipe.recipe_ingredients:
            all_ingredient_names.add(recipe_ingredient.ingredient_name)

        for step in formalized_recipe.steps:
            for step_ingredient in step.step_ingredients:
                all_ingredient_names.add(step_ingredient.ingredient_name)

        logger.info('[FormalizationPipeline] %d ingrédients uniques détectés', len(all_ingredient_names))

        ingredient_map = {}
        created_count = 0

        for ingredient_name in all_ingredient_names:
            ingredient, created = get_or_create_ingredient(ingredient_name)
            if created:
                created_count += 1
            ensure_ingredient_category(ingredient)
            ingredient_map[ingredient_name] = ingredient

        logger.info(
            '[FormalizationPipeline] %d ingrédients résolus (%d créés), catégories assurées',
            len(ingredient_map),
            created_count,
        )

        fields = {
            'title': formalized_recipe.title,
            'description': formalized_recipe.description or '',
            'steps_summary': formalized_recipe.steps_summary,
            'meal_type': formalized_recipe.meal_type,
            'difficulty': formalized_recipe.difficulty,
            'prep_time': formalized_recipe.prep_time,
            'cook_time': formalized_recipe.cook_time,
            'servings': formalized_recipe.servings,
            'image_path': persistable_recipe_image_path(data.get('image_path')),
            'source_type': data.get('source_type', 'user_created'),
            'import_source_url': data.get('import_source_url') or None,
            'search_index_status': Recipe.SearchIndexStatus.PENDING,
        }
        if target is None:
            recipe = Recipe.objects.create(**fields, created_by=user, is_public=True)
        else:
            recipe = target
            if keep_title or not fields['title']:
                fields.pop('title')
            for key in ('image_path', 'import_source_url'):
                if not fields[key]:
                    fields.pop(key)  # on ne vide jamais ce que la recette avait déjà
            for key, value in fields.items():
                setattr(recipe, key, value)
            recipe.save(force_update=True)  # recette supprimée entre-temps (Annuler) : erreur, jamais recréée
            RecipeIngredient.objects.filter(recipe=recipe).delete()
            Step.objects.filter(recipe=recipe).delete()

        # Utiliser un set pour éviter les doublons d'ingrédients
        added_ingredients = set()
        for recipe_ingredient in formalized_recipe.recipe_ingredients:
            ingredient = ingredient_map[recipe_ingredient.ingredient_name]
            # Vérifier si cet ingrédient a déjà été ajouté à la recette
            if ingredient.id in added_ingredients:
                logger.warning(
                    "[FormalizationPipeline] Ingredient '%s' (id=%d) already added to recipe, skipping duplicate",
                    ingredient.name,
                    ingredient.id,
                )
                continue

            RecipeIngredient.objects.create(
                recipe=recipe, ingredient=ingredient, quantity=recipe_ingredient.quantity, unit=recipe_ingredient.unit
            )
            added_ingredients.add(ingredient.id)

        for step_data in formalized_recipe.steps:
            step = Step.objects.create(
                recipe=recipe,
                order=step_data.order,
                title=step_data.title or '',
                instruction=step_data.instruction,
                tip=step_data.tip or '',
                has_timer=step_data.has_timer,
                timer_duration=step_data.timer_duration,
            )

            # Utiliser un set pour éviter les doublons d'ingrédients dans ce step
            step_added_ingredients = set()
            for step_ingredient_data in step_data.step_ingredients:
                ingredient = ingredient_map[step_ingredient_data.ingredient_name]
                # Vérifier si cet ingrédient a déjà été ajouté à ce step
                if ingredient.id in step_added_ingredients:
                    logger.warning(
                        "[FormalizationPipeline] Ingredient '%s' (id=%d) already added to step %d, skipping duplicate",
                        ingredient.name,
                        ingredient.id,
                        step.order,
                    )
                    continue

                StepIngredient.objects.create(
                    step=step,
                    ingredient=ingredient,
                    quantity=step_ingredient_data.quantity,
                    unit=step_ingredient_data.unit,
                )
                step_added_ingredients.add(ingredient.id)

        # Équipements déclarés par la sortie structurée du LLM (même passe) : unis à l'inférence par mots-clés.
        hints = getattr(formalized_recipe, 'required_equipment', None)
        if hints:
            set_ai_hints(recipe, hints)

    schedule_recipe_search_reindex(recipe.id)
    return recipe
