from typing import Dict, Any, Optional

from .ingredient_matcher import get_batch_embeddings


def _format_recipe_text(formalized_recipe, data: Dict[str, Any]) -> str:
    parts = [
        formalized_recipe.title or '',
        formalized_recipe.description or '',
    ]

    ingredients_lines = []
    for recipe_ingredient in formalized_recipe.recipe_ingredients:
        qty = recipe_ingredient.quantity or ''
        unit = recipe_ingredient.unit or ''
        line = f"{qty} {unit} {recipe_ingredient.ingredient_name}".strip()
        ingredients_lines.append(line)

    parts.append("Ingredients:")
    parts.extend(ingredients_lines)

    step_lines = []
    for step in formalized_recipe.steps:
        title = step.title or f"Step {step.order}"
        instruction = step.instruction or ''
        line = f"{title}: {instruction}"
        step_lines.append(line)

    parts.append("Steps:")
    parts.extend(step_lines)

    if data.get('categories'):
        parts.append(f"Categories: {', '.join(data['categories'])}")

    return "\n".join(part for part in parts if part)


def generate_recipe_embedding(formalized_recipe, data: Dict[str, Any]) -> Optional[list]:
    """
    Crée un embedding de recette à partir des informations formalisées.
    """
    text = _format_recipe_text(formalized_recipe, data)
    embeddings = get_batch_embeddings([text])
    if embeddings and embeddings[0]:
        return embeddings[0]
    return None



