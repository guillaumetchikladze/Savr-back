"""Classification d'ingrédient : mots-clés → Autres (pas d'appel LLM ici)."""
from __future__ import annotations

from typing import Optional

from recipes.models import Category, Ingredient
from recipes.supermarket_categories import match_leaf_slug_from_name


def _category_slug(ingredient: Ingredient) -> Optional[str]:
    cat = ingredient.category
    if cat is None:
        return None
    return cat.slug or None


def resolve_category_for_ingredient(
    ingredient_name: str,
    ingredient: Ingredient,
    *,
    force: bool = False,
    retry_autres: bool = True,
) -> Optional[Category]:
    """
    Détermine la catégorie feuille pour un ingrédient.
    Ordre : catégorie déjà posée (sauf Autres si retry) → mots-clés → Autres.
    """
    if ingredient.category_id and not force:
        current_slug = _category_slug(ingredient)
        if current_slug and current_slug != 'autres':
            return ingredient.category
        if current_slug == 'autres' and not retry_autres:
            return ingredient.category

    name = (ingredient_name or ingredient.name or '').strip()
    slug = match_leaf_slug_from_name(name)
    if slug:
        cat = Category.objects.filter(slug=slug).first()
        if cat:
            return cat

    return Category.objects.filter(slug='autres').first()


def ensure_ingredient_category(
    ingredient: Ingredient,
    *,
    force: bool = False,
    retry_autres: bool = True,
) -> Optional[Category]:
    """
    Assigne et persiste une catégorie si besoin (null / Autres / force).
    Idempotent : ne touche pas une feuille déjà correctement posée.
    """
    category = resolve_category_for_ingredient(
        ingredient.name,
        ingredient,
        force=force,
        retry_autres=retry_autres,
    )
    if category is None:
        return None
    if ingredient.category_id != category.id:
        ingredient.category = category
        ingredient.save(update_fields=['category'])
    return category
