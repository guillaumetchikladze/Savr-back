"""
Résolution de la politique de version (DB → cache → fallback settings) et calcul du statut.

Fail-open partout : plateforme inconnue, version illisible, base indisponible → « ok ».
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

from .versioning import parse_version

logger = logging.getLogger(__name__)

PLATFORMS = ('ios', 'android')
STATUS_OK = 'ok'
STATUS_UPDATE_AVAILABLE = 'update_available'
STATUS_UPDATE_REQUIRED = 'update_required'

CACHE_TTL_SECONDS = 60
_CACHE_PREFIX = 'appversion:policy:'
_NO_POLICY = '__none__'


def cache_key(platform: str) -> str:
    return f'{_CACHE_PREFIX}{platform}'


def invalidate_policy_cache(platform: str | None = None) -> None:
    cache.delete_many([cache_key(p) for p in ([platform] if platform else PLATFORMS)])


def normalize_platform(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in PLATFORMS else None


def _policy_from_settings(platform: str) -> dict | None:
    suffix = platform.upper()
    latest = getattr(settings, f'APP_LATEST_VERSION_{suffix}', '') or ''
    if not latest:
        return None
    return {
        'latest_version': latest,
        'min_supported_version': getattr(settings, f'APP_MIN_SUPPORTED_VERSION_{suffix}', '') or latest,
        'store_url': getattr(settings, f'APP_STORE_URL_{suffix}', '') or None,
        'message_update_available': None,
        'message_update_required': None,
    }


def _policy_from_db(platform: str) -> dict | None:
    from .models import AppVersionPolicy

    row = AppVersionPolicy.objects.filter(platform=platform).first()
    if row is None:
        return None
    return {
        'latest_version': row.latest_version,
        'min_supported_version': row.min_supported_version,
        'store_url': row.store_url or None,
        'message_update_available': row.message_update_available or None,
        'message_update_required': row.message_update_required or None,
    }


def get_policy(platform: str) -> dict | None:
    """Politique de la plateforme (dict) ou None. Mise en cache courte, invalidée à l'enregistrement."""
    key = cache_key(platform)
    cached = cache.get(key)
    if cached == _NO_POLICY:
        return _policy_from_settings(platform)
    if cached is not None:
        return cached
    try:
        policy = _policy_from_db(platform)
    except Exception:
        logger.exception('appversion: lecture de la politique impossible (%s), fail-open', platform)
        return _policy_from_settings(platform)
    cache.set(key, policy if policy is not None else _NO_POLICY, CACHE_TTL_SECONDS)
    return policy if policy is not None else _policy_from_settings(platform)


def _empty_payload() -> dict:
    return {
        'status': STATUS_OK,
        'latest_version': None,
        'min_supported_version': None,
        'store_url': None,
        'message': None,
    }


def evaluate(platform, version) -> dict:
    """Payload de `/api/app/version-check/` (aussi utilisé par le middleware 426)."""
    platform = normalize_platform(platform)
    if platform is None:
        return _empty_payload()
    policy = get_policy(platform)
    if not policy:
        return _empty_payload()

    payload = {
        'status': STATUS_OK,
        'latest_version': policy['latest_version'],
        'min_supported_version': policy['min_supported_version'],
        'store_url': policy['store_url'],
        'message': None,
    }
    current = parse_version(version)
    if current is None:
        return payload

    minimum = parse_version(policy['min_supported_version'])
    latest = parse_version(policy['latest_version'])
    if minimum is not None and current < minimum:
        payload['status'] = STATUS_UPDATE_REQUIRED
        payload['message'] = policy['message_update_required']
    elif latest is not None and current < latest:
        payload['status'] = STATUS_UPDATE_AVAILABLE
        payload['message'] = policy['message_update_available']
    return payload
