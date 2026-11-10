"""Tâches Celery stats : classification des nouveaux ingrédients, badges, notifications de badge, rareté."""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache

from core.celery_dispatch import publish

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
            publish(classify_new_ingredients, countdown=CLASSIFY_DEBOUNCE_SECONDS)
    except Exception:
        logger.warning('[stats] classification ingrédients non planifiée', exc_info=True)


def schedule_badge_evaluation(user_id: int) -> None:
    """Évaluation des badges en différé (chemins « cuisiné » hors POST /posts/cooked/). Ne lève jamais."""
    try:
        if cache.add(f'stats:badges:pending:{user_id}', 1, BADGES_DEBOUNCE_SECONDS):
            publish(evaluate_user_badges, args=[user_id], countdown=BADGES_DEBOUNCE_SECONDS)
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
    """
    Évaluation différée (après un « cuisiné ») ; avec `WEATHER_BADGES_ENABLED` et une ville déclarée, complète
    d'abord le cache météo des jours cuisinés (réseau : ici seulement) puis évalue aussi les badges météo.
    """
    from .services import weather
    from .services.badges import evaluate_badges, lifetime_stats

    cache.delete(f'stats:badges:pending:{user_id}')
    user = get_user_model().objects.filter(pk=user_id).first()
    if not user:
        return 0
    stats = lifetime_stats(user)
    with_weather = weather.enabled() and bool(user.city)
    if with_weather:
        weather.refresh_user_weather(user, {m.date for m in stats.meals})
    return len(evaluate_badges(user, stats=stats, include_async=with_weather))


@shared_task(ignore_result=True)
def geocode_user_city_task(user_id: int) -> int:
    """Ville déclarée → coordonnées (une fois), puis badges météo sur tout l'historique (sans rattrapage notifié)."""
    from .services import weather

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None or not weather.enabled() or not user.city:
        return 0
    return evaluate_user_badges.run(user_id)


@shared_task(ignore_result=True)
def send_new_badge_push(user_id: int, codes: list[str], push: bool = True) -> None:
    """
    Une notification par évaluation : « Badge secret débloqué : Raclette en juillet » (`badge_rare`) si l'un
    des nouveaux badges est rare ou secret, sinon « Nouveau badge » (`new_badge`). In-app + push catégorie
    `rewards` (si `push` et BADGE_PUSH_ENABLED), plafond quotidien du registre.
    """
    from notifications.services import notify

    from .services.badge_rarity import is_special, rarity_map
    from .services.badges import BADGES_BY_CODE

    badges = [BADGES_BY_CODE[c] for c in codes if c in BADGES_BY_CODE]
    user = get_user_model().objects.filter(pk=user_id).first()
    if not badges or user is None:
        return
    rmap = rarity_map()
    special = [b for b in badges if is_special(b, rmap)]
    # Les secrets d'abord, puis les plus rares : c'est eux qu'on annonce.
    first = special[0] if special else badges[0]
    if special:
        first = next((b for b in special if b.secret), first)
    ordered = [first.code] + [b.code for b in badges if b.code != first.code]
    notify(
        'badge_rare' if special else 'new_badge',
        user,
        context={
            'badge': first.title,
            'emoji': first.emoji,
            'description': first.description,
            'phrase': first.phrase,
            'secret': first.secret,
            'others': len(badges) - 1,
        },
        data={'badge_codes': ordered, 'badge_code': first.code, 'secret': first.secret},
        send_push=push and getattr(settings, 'BADGE_PUSH_ENABLED', True),
    )


@shared_task(ignore_result=True)
def compute_badge_rarity_task() -> int:
    """Beat quotidien `stats-badge-rarity` : rareté de chaque badge (% des cuisiniers actifs)."""
    from .services.badge_rarity import compute_badge_rarity

    return len(compute_badge_rarity())
