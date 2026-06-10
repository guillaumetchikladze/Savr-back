"""Rate limiting et locks Redis pour le chat."""

import logging
from datetime import timedelta

import redis
from decouple import config
from django.utils import timezone

logger = logging.getLogger(__name__)

REDIS_URL = config('CHANNEL_REDIS_URL', default='redis://localhost:6379/1')

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def check_message_rate_limit(user_id: int, *, max_per_minute: int = 10) -> bool:
    """Retourne True si l'utilisateur peut envoyer un message."""
    try:
        key = f'chat_rate_{user_id}'
        client = _get_client()
        count = client.incr(key)
        if count == 1:
            client.expire(key, 60)
        return count <= max_per_minute
    except Exception:
        logger.warning('Redis indisponible — rate limit chat désactivé')
        return True


def acquire_stream_lock(conversation_id: int, *, ttl_seconds: int = 120) -> bool:
    """Acquiert un lock pour un seul stream actif par conversation."""
    try:
        key = f'chat_stream_{conversation_id}'
        client = _get_client()
        return bool(client.set(key, '1', nx=True, ex=ttl_seconds))
    except Exception:
        logger.warning('Redis indisponible — lock stream chat désactivé')
        return True


def release_stream_lock(conversation_id: int):
    key = f'chat_stream_{conversation_id}'
    try:
        _get_client().delete(key)
    except Exception:
        logger.warning('Failed to release stream lock conv=%s', conversation_id)


def default_action_expiry():
    return timezone.now() + timedelta(hours=24)
