"""Recherche hybride recettes : pg_trgm + pgvector en une requête SQL."""

from __future__ import annotations

from typing import List, Optional

from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity, TrigramWordSimilarity
from django.db.models import Case, F, FloatField, Q, Value, When
from django.db.models.expressions import ExpressionWrapper
from django.db.models.functions import Coalesce
from pgvector.django import CosineDistance


def hybrid_recipe_queryset(base_qs, query: str, query_vector: Optional[List[float]] = None):
    """
    Hybride sémantique + trigram.
    - Titre : similarité classique (champ court).
    - search_index_text : word_similarity (mot le plus proche, ex. healty → healthy dans les tags).
    - Si query_vector est None (API embedding down), recherche trigram uniquement.
    """
    w_sem = settings.SEARCH_HYBRID_WEIGHT_SEMANTIC
    w_trg = settings.SEARCH_HYBRID_WEIGHT_TRIGRAM
    max_d = settings.SEARCH_SEMANTIC_MAX_DISTANCE
    min_trgm = settings.SEARCH_TRIGRAM_MIN_SCORE

    if query_vector is None:
        distance_annotation = Value(None, output_field=FloatField(null=True))
    else:
        distance_annotation = Case(
            When(embedding__isnull=True, then=Value(None)),
            default=CosineDistance('embedding', query_vector),
            output_field=FloatField(null=True),
        )

    qs = base_qs.annotate(
        trgm_title=TrigramSimilarity('title', query),
        # WORD_SIMILARITY(query, text) : match un mot du texte d'index (tags inclus)
        trgm_index_word=TrigramWordSimilarity(query, 'search_index_text'),
        distance=distance_annotation,
    ).annotate(
        trgm_score=ExpressionWrapper(
            Coalesce(F('trgm_title'), 0.0) + Coalesce(F('trgm_index_word'), 0.0) * Value(0.85),
            output_field=FloatField(),
        ),
        semantic_score=Case(
            When(embedding__isnull=True, then=Value(0.0)),
            When(distance__isnull=True, then=Value(0.0)),
            When(distance__gte=max_d, then=Value(0.0)),
            default=1.0 - F('distance') / 2.0,
            output_field=FloatField(),
        ),
        hybrid_score=ExpressionWrapper(
            F('semantic_score') * Value(w_sem) + F('trgm_score') * Value(w_trg),
            output_field=FloatField(),
        ),
    ).filter(
        Q(hybrid_score__gte=settings.SEARCH_HYBRID_MIN_SCORE)
        | Q(trgm_title__gte=min_trgm)
        | Q(trgm_index_word__gte=min_trgm)
    ).order_by('-hybrid_score', '-trgm_score')

    return qs


def fuzzy_recipe_queryset(base_qs, query: str):
    """
    Recherche rapide : trigram sur le titre uniquement.
    Évite search_index_text (lent sur toute la table) et les embeddings.
    """
    min_trgm = settings.SEARCH_TRIGRAM_MIN_SCORE
    return base_qs.annotate(
        trgm_title=TrigramSimilarity('title', query),
    ).filter(
        Q(trgm_title__gte=min_trgm) | Q(title__icontains=query)
    ).order_by('-trgm_title', '-created_at')
