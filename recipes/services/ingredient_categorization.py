"""Classification d'ingrédient : mots-clés → Autres (pas d'appel LLM ici)."""
from __future__ import annotations

from typing import Optional

from recipes.models import Category, Ingredient
from recipes.supermarket_categories import match_leaf_slug_from_name


def resolve_category_for_ingredient(ingredient_name: str, ingredient: Ingredient) -> Optional[Category]:
    """
    Détermine la catégorie feuille pour un ingrédient.
    Ordre : catégorie déjà posée → mots-clés → Autres.
    """
    if ingredient.category_id:
        return ingredient.category

    slug = match_leaf_slug_from_name(ingredient_name)
    if slug:
        cat = Category.objects.filter(slug=slug).first()
        if cat:
            return cat

    return Category.objects.filter(slug='autres').first()
