"""Tâches Celery stats : classification des nouveaux ingrédients, badges, notification « Nouveau badge »."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache

logger = logging.getLogger(__name__)

CLASSIFY_DEBOUNCE_KEY = 'stats:classify-ingredients:pending'
CLASSIFY_DEBOUNCE_SECONDS = 120
CLASSIFY_MAX_PER_RUN = 200
BADGES_DEBOUNCE_SECONDS = 30


def schedule_ingredient_classification() -> None:
    """Planifie (au plus une fois par fenêtre) la classification des ingrédients sans profil. Ne lève jamais."""
    if not getattr(settings, 'STATS_AUTO_CLASSIFY_INGREDIENTS', True):
        return
    try:
        if cache.add(CLASSIFY_DEBOUNCE_KEY, 1, CLASSIFY_DEBOUNCE_SECONDS):
            classify_new_ingredients.apply_async(countdown=CLASSIFY_DEBOUNCE_SECONDS, retry=False)
    except Exception:
        logger.warning('[stats] classification ingrédients non planifiée', exc_info=True)


def schedule_badge_evaluation(user_id: int) -> None:
    """Évaluation des badges en différé (chemins « cuisiné » hors POST /posts/cooked/). Ne lève jamais."""
    try:
        if cache.add(f'stats:badges:pending:{user_id}', 1, BADGES_DEBOUNCE_SECONDS):
            evaluate_user_badges.apply_async(args=[user_id], countdown=BADGES_DEBOUNCE_SECONDS, retry=False)
    except Exception:
        logger.warning('[stats] évaluation badges non planifiée user=%s', user_id, exc_info=True)


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


@shared_task(ignore_result=True)
def evaluate_user_badges(user_id: int) -> int:
    from .services.badges import evaluate_badges

    cache.delete(f'stats:badges:pending:{user_id}')
    user = get_user_model().objects.filter(pk=user_id).first()
    if not user:
        return 0
    return len(evaluate_badges(user))


@shared_task(ignore_result=True)
def send_new_badge_push(user_id: int, codes: list[str], push: bool = True) -> None:
    """In-app « Nouveau badge » (+ push catégorie `rewards`, si `push` et BADGE_PUSH_ENABLED)."""
    from notifications.services import notify

    from .services.badges import BADGES_BY_CODE

    badges = [BADGES_BY_CODE[c] for c in codes if c in BADGES_BY_CODE]
    user = get_user_model().objects.filter(pk=user_id).first()
    if not badges or user is None:
        return
    first = badges[0]
    notify(
        'new_badge',
        user,
        context={
            'badge': first.title,
            'emoji': first.emoji,
            'description': first.description,
            'others': len(badges) - 1,
        },
        data={'badge_codes': [b.code for b in badges]},
        send_push=push and getattr(settings, 'BADGE_PUSH_ENABLED', True),
    )
