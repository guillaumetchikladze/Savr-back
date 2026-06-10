"""Service sync de recherche sémantique de recettes."""

import logging

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Q

from recipes.dietary_filters import conflict_reasons_by_recipe_id
from recipes.models import Recipe
from recipes.services.ingredient_matcher import get_batch_embeddings
from recipes.services.recipe_search import hybrid_recipe_queryset

from chat.services.tool_schemas import RecipeSummary, SearchRecipesResult

logger = logging.getLogger(__name__)


def _recipe_base_queryset(user: AbstractBaseUser):
    return Recipe.objects.filter(
        Q(is_public=True) | Q(created_by=user)
    ).defer(
        'description',
        'created_at',
        'updated_at',
        'created_by_id',
        'search_index_text',
        'search_context_tags',
        'search_index_hash',
    ).order_by('-created_at')


def _penalty(reasons):
    if not reasons:
        return 0
    if 'allergy' in reasons:
        return 3
    if 'diet' in reasons:
        return 2
    if 'dislike' in reasons:
        return 1
    return 0


def search_recipes_for_user(
    user: AbstractBaseUser,
    query: str,
    *,
    limit: int = 10,
) -> SearchRecipesResult:
    query = (query or '').strip()
    if not query:
        return SearchRecipesResult(query='', count=0, recipes=[])

    limit = min(max(limit, 1), 20)
    embeddings = get_batch_embeddings([query], input_type='query')
    vector = embeddings[0] if embeddings else None
    if not vector:
        logger.warning('[search_recipes_for_user] embedding indisponible pour q=%r', query)

    base_qs = _recipe_base_queryset(user)
    queryset = hybrid_recipe_queryset(base_qs, query, vector)
    items = list(queryset[:limit * 2])

    try:
        ids = [r.id for r in items if getattr(r, 'id', None) is not None]
        reasons_map = conflict_reasons_by_recipe_id(ids, user) or {}
        indexed = [(idx, r, _penalty(reasons_map.get(r.id))) for idx, r in enumerate(items)]
        indexed.sort(key=lambda t: (t[2], t[0]))
        items = [t[1] for t in indexed]
    except Exception:
        pass

    items = items[:limit]
    summaries = [
        RecipeSummary(
            id=r.id,
            title=r.title,
            prep_time=r.prep_time,
            cook_time=r.cook_time,
            difficulty=r.difficulty,
            image_url=getattr(r, 'image_url', None) or getattr(r, 'thumbnail_url', None),
        )
        for r in items
    ]
    return SearchRecipesResult(query=query, count=len(summaries), recipes=summaries)
