"""
GET/PATCH /api/notifications/preferences/ — réglages par catégorie (+ surcharges par type,
heures calmes, fuseau) ; POST /api/notifications/opened/ — suivi d'ouverture des push.
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated

from .models import NotificationPreference
from .registry import CATEGORIES, LEGACY_PREFERENCE_KEYS, REGISTRY, configurable_categories
from .services import category_enabled, daily_cap, get_preferences, mark_opened


def _fmt(t: time) -> str:
    return t.strftime('%H:%M')


def _preferences_payload(prefs: NotificationPreference) -> dict:
    return {
        'categories': [
            {
                'key': c.key,
                'label': c.label,
                'description': c.description,
                'enabled': category_enabled(prefs, c.key),
                'default': c.default_enabled,
            }
            for c in configurable_categories()
        ],
        'types': dict(prefs.types or {}),
        'quiet_hours': {'start': _fmt(prefs.quiet_hours_start), 'end': _fmt(prefs.quiet_hours_end)},
        'timezone': prefs.timezone,
        'daily_cap': daily_cap(),
    }


def _parse_time(value) -> time | None:
    try:
        hh, mm = str(value).split(':')[:2]
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return None


def _validate_patch(data, prefs: NotificationPreference) -> dict:
    """Applique le PATCH sur `prefs` (non sauvegardé) ; renvoie les erreurs éventuelles."""
    errors: dict[str, str] = {}
    if not isinstance(data, dict):
        return {'non_field_errors': 'Objet JSON attendu.'}

    categories = data.get('categories')
    if categories is not None:
        if isinstance(categories, list):  # forme GET renvoyée telle quelle : [{key, enabled}]
            categories = {c.get('key'): c.get('enabled') for c in categories if isinstance(c, dict)}
        if not isinstance(categories, dict):
            errors['categories'] = 'Objet {catégorie: booléen} attendu.'
        else:
            merged = dict(prefs.categories or {})
            for key, value in categories.items():
                cat = CATEGORIES.get(key)
                if cat is None or not cat.user_configurable:
                    errors['categories'] = f'Catégorie inconnue ou non réglable : {key}'
                    break
                if not isinstance(value, bool):
                    errors['categories'] = f'Booléen attendu pour {key}'
                    break
                merged[key] = value
            prefs.categories = merged

    types = data.get('types')
    if types is not None:
        if not isinstance(types, dict):
            errors['types'] = 'Objet {type: booléen} attendu.'
        else:
            merged = dict(prefs.types or {})
            for key, value in types.items():
                targets = LEGACY_PREFERENCE_KEYS.get(key) or ((key,) if key in REGISTRY else ())
                if (
                    not targets
                    or not isinstance(value, bool)
                    or any(not CATEGORIES[REGISTRY[t].category].user_configurable for t in targets)
                ):
                    errors['types'] = f'Type inconnu, non réglable ou valeur non booléenne : {key}'
                    break
                for t in targets:
                    if value:
                        merged.pop(t, None)  # True = défaut : on ne garde que les coupures
                    else:
                        merged[t] = False
            prefs.types = merged

    quiet = data.get('quiet_hours')
    if quiet is not None:
        start = _parse_time(quiet.get('start')) if isinstance(quiet, dict) else None
        end = _parse_time(quiet.get('end')) if isinstance(quiet, dict) else None
        if start is None or end is None:
            errors['quiet_hours'] = 'Format attendu : {"start": "22:00", "end": "08:00"}.'
        else:
            prefs.quiet_hours_start, prefs.quiet_hours_end = start, end

    tz = data.get('timezone')
    if tz is not None:
        try:
            ZoneInfo(str(tz))
            prefs.timezone = str(tz)[:64]
        except (ZoneInfoNotFoundError, ValueError):
            errors['timezone'] = 'Fuseau IANA inconnu (ex. Europe/Paris).'
    return errors


@api_view(['GET', 'PATCH'])
@permission_classes([IsValidated])
def preferences_view(request):
    if request.method == 'GET':
        return Response(_preferences_payload(get_preferences(request.user)))

    with transaction.atomic():
        prefs, _ = NotificationPreference.objects.select_for_update().get_or_create(user=request.user)
        errors = _validate_patch(request.data, prefs)
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        prefs.save()
    return Response(_preferences_payload(prefs))


@api_view(['POST'])
@permission_classes([IsValidated])
def opened_view(request):
    """
    Corps : {"notification_id": 123} (valeur de `data.notification_id` de la push), ou à défaut
    {"type": "friends_digest"} (dernier envoi non ouvert de ce type, 7 jours).
    """
    raw_id = request.data.get('notification_id')
    type_key = request.data.get('type')
    notification_id = None
    if raw_id not in (None, ''):
        try:
            notification_id = int(raw_id)
        except (TypeError, ValueError):
            return Response({'notification_id': 'Entier attendu.'}, status=status.HTTP_400_BAD_REQUEST)
    if notification_id is None and not type_key:
        return Response({'error': 'notification_id ou type requis.'}, status=status.HTTP_400_BAD_REQUEST)
    if type_key and type_key not in REGISTRY:
        return Response({'type': 'Type inconnu.'}, status=status.HTTP_400_BAD_REQUEST)
    tracked = mark_opened(request.user, notification_id=notification_id, type_key=type_key)
    return Response({'tracked': tracked})
