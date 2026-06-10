import logging
from typing import Dict, Any
from django.db import transaction

from ..models import (
    Ingredient,
    Recipe,
    RecipeIngredient,
    Step,
    StepIngredient,
)
from .ai_service import verify_quantity_consistency
from .ingredient_matcher import find_ingredient_by_name
from .recipe_search_index import schedule_recipe_search_reindex

logger = logging.getLogger(__name__)


def create_recipe_from_formalized(formalized_recipe, data: Dict[str, Any], user):
    """
    Reprend l'ancienne logique synchronisée pour créer la recette
    à partir du résultat formalisé.
    """
    inconsistencies = verify_quantity_consistency(formalized_recipe)
    if inconsistencies:
        logger.warning(f"Incohérences de quantités détectées: {inconsistencies}")

    with transaction.atomic():
        # Collecter tous les noms uniques
        all_ingredient_names = set()
        for recipe_ingredient in formalized_recipe.recipe_ingredients:
            all_ingredient_names.add(recipe_ingredient.ingredient_name)

        for step in formalized_recipe.steps:
            for step_ingredient in step.step_ingredients:
                all_ingredient_names.add(step_ingredient.ingredient_name)

        logger.info(
            "[FormalizationPipeline] %d ingrédients uniques détectés",
            len(all_ingredient_names)
        )

        ingredient_map = {}
        ingredients_to_create = []

        for ingredient_name in all_ingredient_names:
            existing = find_ingredient_by_name(ingredient_name)
            if existing:
                ingredient_map[ingredient_name] = existing
            else:
                ingredients_to_create.append(ingredient_name)

        logger.info(
            "[FormalizationPipeline] %d ingrédients trouvés textuellement, %d à créer",
            len(ingredient_map),
            len(ingredients_to_create),
        )

        for ingredient_name in ingredients_to_create:
            ingredient_map[ingredient_name] = Ingredient.objects.create(name=ingredient_name)

        recipe = Recipe.objects.create(
            title=formalized_recipe.title,
            description=formalized_recipe.description or '',
            steps_summary=formalized_recipe.steps_summary,
            meal_type=formalized_recipe.meal_type,
            difficulty=formalized_recipe.difficulty,
            prep_time=formalized_recipe.prep_time,
            cook_time=formalized_recipe.cook_time,
            servings=formalized_recipe.servings,
            image_path=data.get('image_path') or '',
            created_by=user,
            is_public=data.get('is_public', True),
            source_type=data.get('source_type', 'user_created'),
            import_source_url=data.get('import_source_url') or None,
            search_index_status=Recipe.SearchIndexStatus.PENDING,
        )

        # Utiliser un set pour éviter les doublons d'ingrédients
        added_ingredients = set()
        for recipe_ingredient in formalized_recipe.recipe_ingredients:
            ingredient = ingredient_map[recipe_ingredient.ingredient_name]
            # Vérifier si cet ingrédient a déjà été ajouté à la recette
            if ingredient.id in added_ingredients:
                logger.warning(
                    "[FormalizationPipeline] Ingredient '%s' (id=%d) already added to recipe, skipping duplicate",
                    ingredient.name,
                    ingredient.id
                )
                continue
            
            RecipeIngredient.objects.create(
                recipe=recipe,
                ingredient=ingredient,
                quantity=recipe_ingredient.quantity,
                unit=recipe_ingredient.unit
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
                timer_duration=step_data.timer_duration
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
                        step.order
                    )
                    continue
                
                StepIngredient.objects.create(
                    step=step,
                    ingredient=ingredient,
                    quantity=step_ingredient_data.quantity,
                    unit=step_ingredient_data.unit
                )
                step_added_ingredients.add(ingredient.id)

    schedule_recipe_search_reindex(recipe.id)
    return recipe

