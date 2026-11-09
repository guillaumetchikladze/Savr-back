"""Tâches Celery stats : classification différée des nouveaux ingrédients."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CLASSIFY_DEBOUNCE_KEY = 'stats:classify-ingredients:pending'
CLASSIFY_DEBOUNCE_SECONDS = 120
CLASSIFY_MAX_PER_RUN = 200


def schedule_ingredient_classification() -> None:
    """Planifie (au plus une fois par fenêtre) la classification des ingrédients sans profil. Ne lève jamais."""
    if not getattr(settings, 'STATS_AUTO_CLASSIFY_INGREDIENTS', True):
        return
    try:
        if cache.add(CLASSIFY_DEBOUNCE_KEY, 1, CLASSIFY_DEBOUNCE_SECONDS):
            classify_new_ingredients.apply_async(countdown=CLASSIFY_DEBOUNCE_SECONDS, retry=False)
    except Exception:
        logger.warning('[stats] classification ingrédients non planifiée', exc_info=True)


@shared_task(ignore_result=True)
def classify_new_ingredients(limit: int = CLASSIFY_MAX_PER_RUN) -> dict:
    from .services.classification import classify_ingredients, ingredients_to_classify

    cache.delete(CLASSIFY_DEBOUNCE_KEY)
    ingredients = ingredients_to_classify(only_missing=True, limit=limit)
    if not ingredients:
        return {'classified': 0}
    report = classify_ingredients(ingredients)
    logger.info('[stats] %d ingrédients classés (seed=%d, llm=%d)', report.total, report.seeded, report.llm)
    return {'classified': report.total, 'failed_batches': report.failed_batches}
