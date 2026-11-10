"""
Recette qu'un utilisateur peut **utiliser** par son id (panier, « J'ai cuisiné », aperçu de l'ardoise).

Un id de recette reçu du client ne suffit jamais : une recette privée d'un autre ne doit pas entrer dans
le panier ni dans un repas (son titre, ses ingrédients et ses étapes ressortiraient ensuite par le panier,
la liste de courses ou le plan de bataille). Recette utilisable si au moins un de :

- publique ou à soi, ou d'un repas auquel on est invité (mêmes règles que `GET /api/recipes/<id>/`) ;
- déjà dans son panier ou dans un de ses repas (envie reprise, « À mon tour ») ;
- plat d'un post que l'on peut voir (`post_visibility.visible_posts`, cf. « À mon tour »).

Sinon : même réponse qu'un id inexistant (pas d'oracle d'existence).
"""

from __future__ import annotations

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Q

from recipes.models import BasketItem, MealPlanRecipeBatch, Recipe


def usable_recipe(user: AbstractBaseUser, recipe_id: int, *, only: tuple[str, ...] = ()) -> Recipe | None:
    from recipes.utils import get_invited_recipe_filter

    from .post_visibility import visible_posts

    if not user or not getattr(user, 'is_authenticated', False):
        return None
    qs = Recipe.objects.filter(id=recipe_id)
    if only:
        qs = qs.only(*only, 'is_public', 'created_by')
    recipe = qs.first()
    if recipe is None:
        return None
    if recipe.is_public or recipe.created_by_id == user.id:
        return recipe
    if Recipe.objects.filter(get_invited_recipe_filter(user), id=recipe_id).exists():
        return recipe
    if BasketItem.objects.filter(user=user, recipe_id=recipe_id).exists():
        return recipe
    if MealPlanRecipeBatch.objects.filter(meal_plan__user=user, recipe_batch__recipe_id=recipe_id).exists():
        return recipe
    in_visible_post = (
        visible_posts(user)
        .filter(
            Q(recipe_batch__recipe_id=recipe_id)
            | Q(meal_plan__meal_plan_recipe_batches__recipe_batch__recipe_id=recipe_id)
        )
        .exists()
    )
    return recipe if in_visible_post else None
