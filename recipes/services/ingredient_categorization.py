"""Classification d'ingrédient : embedding → mots-clés → Autres (pas d'appel LLM ici)."""
from __future__ import annotations

from typing import Optional

from recipes.models import Category, Ingredient
from recipes.supermarket_categories import match_leaf_slug_from_name


def resolve_category_for_ingredient(ingredient_name: str, ingredient: Ingredient) -> Optional[Category]:
    """
    Détermine la catégorie feuille pour un ingrédient.
    Ordre : catégorie déjà posée → voisin sémantique avec catégorie → mots-clés → Autres.
    """
    if ingredient.category_id:
        return ingredient.category

    if ingredient.embedding is not None:
        try:
            embedding_list = (
                list(ingredient.embedding)
                if hasattr(ingredient.embedding, '__iter__') and not isinstance(ingredient.embedding, (str, bytes))
                else ingredient.embedding
            )
            if embedding_list:
                from recipes.services.ingredient_matcher import find_similar_ingredient

                similar = find_similar_ingredient(ingredient_name, embedding_list)
                if similar and similar.category_id:
                    return similar.category
        except Exception:
            pass

    slug = match_leaf_slug_from_name(ingredient_name)
    if slug:
        cat = Category.objects.filter(slug=slug).first()
        if cat:
            return cat

    return Category.objects.filter(slug='autres').first()
