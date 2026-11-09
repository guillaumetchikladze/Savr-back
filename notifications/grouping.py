"""
Debounce des push groupées (miams, commentaires d'un même post).

Chaque événement crée sa notification in-app tout de suite ; la push, elle, est planifiée
`group_window` secondes plus tard. Un compteur Redis par (utilisateur, type, objet) fait
qu'une rafale d'événements ne produit qu'une seule push, agrégée au moment de l'envoi.
Sans Redis : push immédiate (dégradé, jamais bloquant).
"""

from __future__ import annotations

import logging

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

_redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis | None:
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = getattr(settings, 'CELERY_BROKER_URL', None) or 'redis://localhost:6379/0'
    if not url.startswith('redis'):
        return None
    try:
        client = redis.from_url(url, decode_responses=True, socket_connect_timeout=1)
        client.ping()
        _redis_client = client
        return client
    except Exception:
        logger.warning('[notify] Redis indisponible — push groupée envoyée sans debounce', exc_info=True)
        return None


def _version_key(user_id: int, type_key: str, object_id: int) -> str:
    return f'notif_group_version:{type_key}:{user_id}:{object_id}'


def window_seconds(type_key: str) -> int:
    from .registry import get_type

    override = getattr(settings, 'NOTIFICATIONS_GROUP_WINDOW_SECONDS', None)
    return int(override) if override is not None else get_type(type_key).group_window


def schedule_grouped_push(*, user_id: int, type_key: str, object_id: int) -> None:
    """Planifie (ou replanifie) la push agrégée ; seule la dernière tâche planifiée enverra."""
    from .tasks import send_grouped_push_task

    window = window_seconds(type_key)
    client = _get_redis()
    version = None
    if client is not None:
        try:
            key = _version_key(user_id, type_key, object_id)
            version = int(client.incr(key))
            client.expire(key, window + 120)
        except Exception:
            logger.warning('[notify] incr version groupée en échec — envoi direct', exc_info=True)
            version = None
    if version is None:
        send_grouped_push_task.apply_async(args=[user_id, type_key, object_id, None])
        return
    send_grouped_push_task.apply_async(args=[user_id, type_key, object_id, version], countdown=window)


def is_latest_version(*, user_id: int, type_key: str, object_id: int, version: int | None) -> bool:
    if version is None:
        return True
    client = _get_redis()
    if client is None:
        return True
    try:
        current = client.get(_version_key(user_id, type_key, object_id))
        return current is None or int(current) == int(version)
    except Exception:
        logger.warning('[notify] lecture version groupée en échec — envoi', exc_info=True)
        return True


def clear(*, user_id: int, type_key: str, object_id: int) -> None:
    client = _get_redis()
    if client is None:
        return
    try:
        client.delete(_version_key(user_id, type_key, object_id))
    except Exception:
        logger.warning('[notify] nettoyage version groupée en échec', exc_info=True)
