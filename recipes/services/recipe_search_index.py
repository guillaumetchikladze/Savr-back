"""Indexation recherche : hash, texte FR, embedding 512d."""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Recipe
from .ingredient_matcher import get_batch_embeddings
from .recipe_search_context import generate_recipe_search_context
from .search_context_models import RecipeSearchContext

logger = logging.getLogger(__name__)


def _tag_lists(ctx: RecipeSearchContext | None) -> list[list[str]]:
    if not ctx:
        return []
    return [
        ctx.cuisine_style,
        ctx.meal_moment,
        ctx.dish_type,
        ctx.diet_tags,
        ctx.flavor_profile,
        ctx.season,
        ctx.occasion,
        ctx.main_ingredients,
        ctx.search_phrases,
    ]


def format_recipe_search_text(recipe, ctx: RecipeSearchContext | None = None) -> str:
    parts = [recipe.title or '']
    if recipe.description:
        parts.append(recipe.description.strip())
    if recipe.steps_summary:
        parts.append(recipe.steps_summary.strip())

    ingredient_names = [
        ri.ingredient.name
        for ri in recipe.recipe_ingredients.all()
    ]
    if ingredient_names:
        parts.append('Ingrédients: ' + ', '.join(ingredient_names))

    for tags in _tag_lists(ctx):
        if tags:
            parts.append(', '.join(t.strip() for t in tags if t and str(t).strip()))

    return '\n'.join(p for p in parts if p)


def compute_content_hash(recipe, ctx: RecipeSearchContext | None) -> str:
    payload = '|'.join(
        [
            recipe.title or '',
            recipe.description or '',
            recipe.steps_summary or '',
            recipe.meal_type or '',
            recipe.difficulty or '',
            str(recipe.prep_time),
            str(recipe.cook_time),
            ','.join(
                sorted(
                    ri.ingredient.name
                    for ri in recipe.recipe_ingredients.all()
                )
            ),
            str(_tag_lists(ctx)),
        ]
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def mark_search_pending(recipe_id: int) -> None:
    Recipe.objects.filter(pk=recipe_id).update(
        search_index_status=Recipe.SearchIndexStatus.PENDING,
    )


def schedule_recipe_search_reindex(recipe_id: int, *, force: bool = False) -> None:
    """
    Une seule tâche Celery après commit (évite N reindex lors des bulk create ingrédients/steps).
    """
    if not recipe_id:
        return

    def _enqueue():
        from ..tasks import reindex_recipe_search

        mark_search_pending(recipe_id)
        reindex_recipe_search.delay(recipe_id, force=force)

    transaction.on_commit(_enqueue)


def index_recipe(recipe_id: int, *, force: bool = False) -> bool:
    """Indexe une recette (Gemini + embedding). Ne vide jamais l'embedding existant avant succès."""
    recipe = (
        Recipe.objects.filter(pk=recipe_id)
        .prefetch_related('recipe_ingredients__ingredient')
        .first()
    )
    if not recipe:
        logger.warning('[RecipeSearchIndex] recipe %s introuvable', recipe_id)
        return False

    ctx = generate_recipe_search_context(recipe)
    content_hash = compute_content_hash(recipe, ctx)

    if not force and recipe.search_index_hash == content_hash and recipe.search_index_status == Recipe.SearchIndexStatus.READY:
        logger.info('[RecipeSearchIndex] recipe %s déjà à jour (hash=%s)', recipe_id, content_hash[:12])
        return True

    index_text = format_recipe_search_text(recipe, ctx)
    tags_payload = ctx.model_dump() if ctx else None

    embeddings = get_batch_embeddings([index_text], input_type='passage')
    vector = embeddings[0] if embeddings else None
    if not vector:
        # Garder search_index_text + embedding alignés (pas de texte neuf sans vecteur)
        Recipe.objects.filter(pk=recipe_id).update(
            search_index_status=Recipe.SearchIndexStatus.FAILED,
        )
        logger.error('[RecipeSearchIndex] embedding manquant pour recipe %s', recipe_id)
        return False

    now = timezone.now()
    Recipe.objects.filter(pk=recipe_id).update(
        search_context_tags=tags_payload,
        search_index_text=index_text,
        search_index_hash=content_hash,
        search_indexed_at=now,
        search_index_status=Recipe.SearchIndexStatus.READY,
        embedding=vector,
    )
    logger.info('[RecipeSearchIndex] recipe %s indexée (%d dims)', recipe_id, len(vector))
    return True
