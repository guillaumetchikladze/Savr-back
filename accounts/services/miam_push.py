"""
Agrégation des push « miam » (likes) style Instagram.

Chaque like crée toujours une Notification en base (l'app agrège déjà
l'affichage in-app). Les push, elles, sont debouncées : plusieurs likes
rapides sur le même post → une seule push avec un wording agrégé.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence, Tuple

import redis
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

# Fenêtre d'agrégation : on attend un peu après le dernier like avant d'envoyer.
MIAM_PUSH_DEBOUNCE_SECONDS = 12
# Marge pour inclure le like qui a ouvert le burst (horloge / latence DB).
MIAM_PUSH_SINCE_SKEW_SECONDS = 2

_redis_client: Optional[redis.Redis] = None


def _get_redis() -> Optional[redis.Redis]:
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = getattr(settings, 'CELERY_BROKER_URL', None) or 'redis://localhost:6379/0'
    try:
        _redis_client = redis.from_url(url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception:
        logger.warning('miam_push: Redis indisponible — fallback push immédiat', exc_info=True)
        _redis_client = None
        return None


def _version_key(owner_id: int, post_id: int) -> str:
    return f'miam_push_version:{owner_id}:{post_id}'


def _since_key(owner_id: int, post_id: int) -> str:
    return f'miam_push_since:{owner_id}:{post_id}'


def _ttl_seconds() -> int:
    return MIAM_PUSH_DEBOUNCE_SECONDS + 120


def build_miam_aggregation_texts(usernames: Sequence[str], total_count: int) -> Tuple[str, str]:
    """Même logique que Savr/src/utils/notifications.js (buildMiamAggregationTexts)."""
    safe = [u for u in usernames if u]
    first = safe[0] if safe else None
    second = safe[1] if len(safe) > 1 else None

    if total_count <= 0 or not first:
        return 'Nouveaux miams', 'De nouveaux miams sur ton post.'

    if total_count == 1:
        return 'Nouveau miam sur ton post', f'{first} a mis un miam à ton post.'

    if total_count == 2 and second:
        return (
            'Nouveaux miams sur ton post',
            f'{first} et {second} ont mis un miam à ton post.',
        )

    others_count = max(0, total_count - 2)
    if second and others_count > 0:
        plural = 's' if others_count > 1 else ''
        return (
            'Nouveaux miams sur ton post',
            f'{first}, {second} et {others_count} autre{plural} ont mis un miam à ton post.',
        )

    return (
        'Nouveaux miams sur ton post',
        f"{first} et d'autres amis ont mis un miam à ton post.",
    )


def unique_usernames_newest_first(usernames: Iterable[Optional[str]]) -> list:
    seen = set()
    out = []
    for name in usernames:
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def schedule_post_miam_push(*, owner_id: int, post_id: int) -> None:
    """
    Planifie (ou replanifie) une push agrégée pour ce post.

    Chaque nouvel appel incrémente une version Redis : seule la dernière
    tâche planifiée enverra réellement la notification.
    """
    from accounts.tasks import send_aggregated_post_miam_push

    client = _get_redis()
    if client is None:
        send_aggregated_post_miam_push.delay(owner_id, post_id, None)
        return

    try:
        since_iso = (timezone.now() - timedelta(seconds=MIAM_PUSH_SINCE_SKEW_SECONDS)).isoformat()
        # Début de burst : figé jusqu'à l'envoi (nx=True).
        client.set(_since_key(owner_id, post_id), since_iso, nx=True, ex=_ttl_seconds())
        version = int(client.incr(_version_key(owner_id, post_id)))
        client.expire(_version_key(owner_id, post_id), _ttl_seconds())
    except Exception:
        logger.warning('miam_push: incr version failed — fallback immédiat', exc_info=True)
        send_aggregated_post_miam_push.delay(owner_id, post_id, None)
        return

    send_aggregated_post_miam_push.apply_async(
        args=[owner_id, post_id, version],
        countdown=MIAM_PUSH_DEBOUNCE_SECONDS,
    )
    logger.info(
        'miam_push: scheduled owner=%s post=%s version=%s in %ss',
        owner_id,
        post_id,
        version,
        MIAM_PUSH_DEBOUNCE_SECONDS,
    )


def should_send_version(*, owner_id: int, post_id: int, version: Optional[int]) -> bool:
    """True si cette exécution de tâche est encore la plus récente."""
    if version is None:
        return True
    client = _get_redis()
    if client is None:
        return True
    try:
        current = client.get(_version_key(owner_id, post_id))
        if current is None:
            return True
        return int(current) == int(version)
    except Exception:
        logger.warning('miam_push: version check failed — send anyway', exc_info=True)
        return True


def get_burst_since(*, owner_id: int, post_id: int) -> Optional[datetime]:
    """Horodatage de début du burst courant (ou None si inconnu)."""
    client = _get_redis()
    if client is None:
        return None
    try:
        raw = client.get(_since_key(owner_id, post_id))
        if not raw:
            return None
        dt = parse_datetime(raw)
        if dt is None:
            return None
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt
    except Exception:
        logger.warning('miam_push: read since failed', exc_info=True)
        return None


def clear_burst_state(*, owner_id: int, post_id: int) -> None:
    """Nettoie version + since après un envoi réussi."""
    client = _get_redis()
    if client is None:
        return
    try:
        client.delete(_version_key(owner_id, post_id), _since_key(owner_id, post_id))
    except Exception:
        logger.warning('miam_push: clear burst state failed', exc_info=True)
