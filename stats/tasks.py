"""Tâches Celery stats : classification des nouveaux ingrédients, badges, push « Nouveau badge »."""

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
def send_new_badge_push(user_id: int, codes: list[str]) -> None:
    from accounts.models import PushDevice
    from accounts.services.expo_push import send_expo_push_notifications

    from .services.badges import BADGES_BY_CODE

    badges = [BADGES_BY_CODE[c] for c in codes if c in BADGES_BY_CODE]
    tokens = list(
        PushDevice.objects.filter(user_id=user_id, is_active=True)
        .exclude(expo_push_token='')
        .values_list('expo_push_token', flat=True)
    )
    if not badges or not tokens:
        return
    first = badges[0]
    title = f'Nouveau badge : {first.title} {first.emoji}'
    body = first.description if len(badges) == 1 else f'Et {len(badges) - 1} autre(s) badge(s) débloqué(s) !'
    send_expo_push_notifications(
        {'to': t, 'title': title, 'body': body, 'sound': 'default', 'data': {'kind': 'new_badge', 'codes': codes}}
        for t in tokens
    )
