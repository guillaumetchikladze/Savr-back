"""
Notifications de rétention planifiées (Celery beat, toutes les heures, idempotentes) :

- digest « Du nouveau chez tes amis » : au plus 1 par jour, seulement s'il y a au moins un
  nouveau post publié par une personne suivie depuis le dernier digest envoyé, vers 18:00 locale ;
- récap hebdo « Ta semaine à table » : le dimanche vers 19:00 locale, seulement si l'utilisateur
  a cuisiné au moins un plat ou si ses amis en ont partagé ;
- tablées « Cette semaine, 3 sur 5 ont cuisiné dans « Les colocs » » : même beat que le récap (dimanche,
  `NOTIFICATIONS_TABLES_WEEKLY_HOUR`), 1 par semaine au plus, jamais quand personne n'a cuisiné ;
- « Je l'ai déjà » : rappel du soir (`NOTIFICATIONS_COOKED_PENDING_HOUR`, 21:00) d'un repas planifié du jour
  passé sans confirmation, 1 par soir au plus, jamais un repas refusé (« Pas fait ») ni déjà relancé ;
- jamais de message de manque ou de perte (« tu vas perdre ta série », « ça fait longtemps »).

Le plafond quotidien et les heures calmes sont appliqués par `services.push()`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from accounts.blocks import blocked_user_ids_for
from accounts.models import Follow, PushDevice

from .models import NotificationDelivery, NotificationPreference
from .registry import get_type
from .services import PushResult, category_enabled, get_preferences, local_now, push

logger = logging.getLogger(__name__)

DIGEST_DEFAULT_LOOKBACK = timedelta(hours=24)
DIGEST_MAX_LOOKBACK = timedelta(days=3)
DIGEST_MAX_IDS = 10


def _candidates(now: datetime, *, hour: int, weekday: int | None = None):
    """Utilisateurs avec un appareil actif dont l'heure locale (et le jour) correspond."""
    User = get_user_model()
    user_ids = set(
        PushDevice.objects.filter(is_active=True).exclude(expo_push_token='').values_list('user_id', flat=True)
    )
    if not user_ids:
        return
    prefs_by_user = {p.user_id: p for p in NotificationPreference.objects.filter(user_id__in=user_ids)}
    for user in User.objects.filter(id__in=user_ids, is_active=True).order_by('id'):
        prefs = prefs_by_user.get(user.id) or NotificationPreference(user_id=user.id)
        local = local_now(prefs, now)
        if local.hour != hour or (weekday is not None and local.weekday() != weekday):
            continue
        yield user, prefs


def _following_ids(user) -> set[int]:
    ids = set(Follow.objects.filter(follower=user).values_list('following_id', flat=True))
    return ids - blocked_user_ids_for(user) - {user.id}


# ---------------------------------------------------------------------------
# Digest des posts des amis
# ---------------------------------------------------------------------------
def friends_digest_for_user(user, *, now: datetime | None = None) -> PushResult:
    from recipes.models import Post
    from recipes.services.cooked_service import post_display_title

    now = now or timezone.now()
    prefs = get_preferences(user)
    if not category_enabled(prefs, get_type('friends_digest').category):
        return PushResult('muted')
    following = _following_ids(user)
    if not following:
        return PushResult('no_posts')

    last = (
        NotificationDelivery.objects.filter(
            user_id=user.id, type='friends_digest', status=NotificationDelivery.STATUS_SENT
        )
        .order_by('-created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    since = max(last or (now - DIGEST_DEFAULT_LOOKBACK), now - DIGEST_MAX_LOOKBACK)
    posts = list(
        Post.objects.filter(is_published=True, user_id__in=following, created_at__gt=since, created_at__lte=now)
        .select_related('user', 'meal_plan', 'recipe_batch__recipe')
        .order_by('-created_at')
    )
    if not posts:
        return PushResult('no_posts')

    authors: list = []
    for p in posts:
        if p.user not in authors:
            authors.append(p.user)
    ctx = {
        'actors': [a.username for a in authors],
        'author_count': len(authors),
        'post_count': len(posts),
        'dish_title': post_display_title(posts[0]) if len(posts) == 1 else '',
    }
    data = {
        'post_ids': [p.id for p in posts[:DIGEST_MAX_IDS]],
        'author_ids': [a.id for a in authors[:DIGEST_MAX_IDS]],
    }
    if len(posts) == 1:
        data['post_id'] = posts[0].id
    day = local_now(prefs, now).date().isoformat()
    return push('friends_digest', user, context=ctx, data=data, dedupe_key=f'friends_digest:{day}', now=now)


def send_friends_digests(now: datetime | None = None) -> dict:
    now = now or timezone.now()
    hour = int(getattr(settings, 'NOTIFICATIONS_DIGEST_HOUR', 18))
    report: dict[str, int] = {}
    for user, _prefs in _candidates(now, hour=hour):
        try:
            status = friends_digest_for_user(user, now=now).status
        except Exception:
            logger.exception('[notify] digest amis en échec user=%s', user.id)
            status = 'error'
        report[status] = report.get(status, 0) + 1
    return report


# ---------------------------------------------------------------------------
# Récap hebdo
# ---------------------------------------------------------------------------
def weekly_recap_for_user(user, *, now: datetime | None = None) -> PushResult:
    from recipes.models import Post
    from stats.services.stats_service import week_cooking_summary

    now = now or timezone.now()
    prefs = get_preferences(user)
    if not category_enabled(prefs, get_type('weekly_recap').category):
        return PushResult('muted')
    local = local_now(prefs, now)
    week_start = local.date() - timedelta(days=local.weekday())
    week_end = week_start + timedelta(days=6)
    summary = week_cooking_summary(user, week_start, week_end)

    start_dt = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=local.weekday())
    following = _following_ids(user)
    friends_posts = (
        Post.objects.filter(is_published=True, user_id__in=following, created_at__gte=start_dt, created_at__lte=now)
        .exclude(user=user)
        .count()
        if following
        else 0
    )
    if summary['cooked'] == 0 and friends_posts == 0:
        return PushResult('nothing_to_say')

    data = {
        'week_start': week_start.isoformat(),
        'cooked': summary['cooked'],
        'new_dishes': summary['new_dishes'],
        'friends_posts': friends_posts,
    }
    iso = week_start.isocalendar()
    return push(
        'weekly_recap',
        user,
        context=data,
        data=data,
        dedupe_key=f'weekly_recap:{iso.year}-W{iso.week:02d}',
        now=now,
    )


def send_weekly_recaps(now: datetime | None = None) -> dict:
    now = now or timezone.now()
    hour = int(getattr(settings, 'NOTIFICATIONS_WEEKLY_RECAP_HOUR', 19))
    weekday = int(getattr(settings, 'NOTIFICATIONS_WEEKLY_RECAP_WEEKDAY', 6))
    report: dict[str, int] = {}
    for user, _prefs in _candidates(now, hour=hour, weekday=weekday):
        try:
            status = weekly_recap_for_user(user, now=now).status
        except Exception:
            logger.exception('[notify] récap hebdo en échec user=%s', user.id)
            status = 'error'
        report[status] = report.get(status, 0) + 1
    return report


def send_weekly_recaps_and_tables(now: datetime | None = None) -> dict:
    """Beat du dimanche : récap hebdo puis push des tablées (même tâche, même créneau horaire par défaut)."""
    now = now or timezone.now()
    report = {f'recap_{k}': v for k, v in send_weekly_recaps(now).items()}
    report.update({f'tables_{k}': v for k, v in send_tables_weekly(now).items()})
    return report


# ---------------------------------------------------------------------------
# Tablées : « Cette semaine, 3 sur 5 ont cuisiné dans « Les colocs » »
# ---------------------------------------------------------------------------
def tables_weekly_for_user(user, *, now: datetime | None = None) -> PushResult:
    from tables.services import best_table_week

    now = now or timezone.now()
    prefs = get_preferences(user)
    if not category_enabled(prefs, get_type('table_weekly').category):
        return PushResult('muted')
    local = local_now(prefs, now)
    week_start = local.date() - timedelta(days=local.weekday())
    best = best_table_week(user, week_start, week_start + timedelta(days=6))
    if best is None:
        return PushResult('nothing_to_say')  # aucune tablée, ou personne n'a cuisiné : jamais de « 0 sur 5 »
    data = {
        'table_id': best['table_id'],
        'week_start': week_start.isoformat(),
        'cooked_members': best['cooked'],
        'member_count': best['total'],
    }
    iso = week_start.isocalendar()
    return push(
        'table_weekly',
        user,
        context={**data, 'table': best['table']},
        data=data,
        dedupe_key=f'table_weekly:{iso.year}-W{iso.week:02d}',
        now=now,
    )


def send_tables_weekly(now: datetime | None = None) -> dict:
    now = now or timezone.now()
    hour = int(getattr(settings, 'NOTIFICATIONS_TABLES_WEEKLY_HOUR', 19))
    weekday = int(getattr(settings, 'NOTIFICATIONS_WEEKLY_RECAP_WEEKDAY', 6))
    report: dict[str, int] = {}
    for user, _prefs in _candidates(now, hour=hour, weekday=weekday):
        try:
            status = tables_weekly_for_user(user, now=now).status
        except Exception:
            logger.exception('[notify] tablées hebdo en échec user=%s', user.id)
            status = 'error'
        report[status] = report.get(status, 0) + 1
    return report


# ---------------------------------------------------------------------------
# « Je l'ai déjà » : « Tu as cuisiné les pâtes au pesto ? » le soir
# ---------------------------------------------------------------------------
def cooked_pending_for_user(user, *, now: datetime | None = None) -> PushResult:
    """
    1 push par soir au plus (une `NotificationDelivery` `cooked_pending` depuis minuit local), pour le repas
    du jour le plus récent de `pending_confirmations` (créneau passé, non confirmé, pas « Pas fait ») jamais
    encore relancé : même clé `cooked_prompt:{meal_plan_id}` que la push de fin de cuisson, donc jamais deux
    fois le même repas.
    """
    from recipes.services.pending_confirmations import pending_confirmations

    now = now or timezone.now()
    prefs = get_preferences(user)
    if not category_enabled(prefs, get_type('cooked_pending').category):
        return PushResult('muted')
    midnight = local_now(prefs, now).replace(hour=0, minute=0, second=0, microsecond=0)
    if NotificationDelivery.objects.filter(user_id=user.id, type='cooked_pending', created_at__gte=midnight).exists():
        return PushResult('duplicate')  # déjà une relance ce soir (envoyée, plafonnée ou heures calmes)
    items = pending_confirmations(user, now=now, days=1, limit=10)
    if not items:
        return PushResult('nothing_pending')
    asked = set(
        NotificationDelivery.objects.filter(
            user_id=user.id, dedupe_key__in=[f'cooked_prompt:{i.meal_plan.id}' for i in items]
        ).values_list('dedupe_key', flat=True)
    )
    item = next((i for i in items if f'cooked_prompt:{i.meal_plan.id}' not in asked), None)
    if item is None:
        return PushResult('duplicate')
    mp = item.meal_plan
    data = {
        'meal_plan_id': mp.id,
        'basket_item_id': item.basket_item.id if item.basket_item is not None else None,
        'pending_kind': item.kind,
        'pending_id': item.id,
        # 1 tap « Oui, je l'ai cuisiné » → POST /posts/cooked/ {meal_plan_id} (app 1.0.9 comprise)
        'action': 'cooked_prompt',
        'title': item.title,
        'recipe_ids': item.recipe_ids,
        'recipe_id': item.recipe_ids[0] if item.recipe_ids else None,
        'date': mp.date.isoformat(),
        'meal_time': mp.meal_time,
    }
    return push(
        'cooked_pending',
        user,
        context={'dish_title': item.title},
        data=data,
        dedupe_key=f'cooked_prompt:{mp.id}',
        now=now,
    )


def send_cooked_pending_reminders(now: datetime | None = None) -> dict:
    now = now or timezone.now()
    hour = int(getattr(settings, 'NOTIFICATIONS_COOKED_PENDING_HOUR', 21))
    report: dict[str, int] = {}
    for user, _prefs in _candidates(now, hour=hour):
        try:
            status = cooked_pending_for_user(user, now=now).status
        except Exception:
            logger.exception("[notify] rappel « Je l'ai déjà » en échec user=%s", user.id)
            status = 'error'
        report[status] = report.get(status, 0) + 1
    return report
