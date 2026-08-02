"""Feature entitlement helpers for paywall / plan gating."""

from __future__ import annotations

from django.conf import settings


def user_has_feature(user, feature_name: str) -> bool:
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if hasattr(user, 'has_feature'):
        return bool(user.has_feature(feature_name))
    if feature_name == 'ai':
        if not getattr(settings, 'AI_PAYWALL_ENABLED', False):
            return True
        return getattr(user, 'plan', None) == 'premium'
    return False


def feature_locked_payload(feature_name: str = 'ai') -> dict:
    return {
        'error': 'Fonctionnalité premium requise.',
        'code': 'feature_locked',
        'feature': feature_name,
    }
