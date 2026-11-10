"""
Endpoints stats / badges / Récap.

GET /api/stats/me/?period=week|month|year|all&year=2026
GET /api/stats/users/{id}/?period=…  (sous-ensemble public + badges obtenus, mêmes règles que le profil)
GET /api/badges/me/[?all=1]  (all=1 : tous les badges verrouillés avec progression dans `next`, secrets en « ??? »)
GET /api/badges/<code>/share/ (carte à partager d'un badge obtenu : badge, rareté, nom, lien d'invitation perso)
GET|PUT|DELETE /api/weather/city/ (badges météo : ville déclarée, opt-in, géocodée en tâche)
GET /api/recap/{year}/        (mis en cache, cf. recap.get_recap)
GET /api/recap/{year}/status/ (disponibilité seule, sans construire le Récap)
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated
from accounts.privacy import can_view_profile_content

from .services.badges import badge_share_payload, badges_overview, earned_badges_public
from .services.recap import get_recap, is_recap_available, recap_available_from, recap_status
from .services.stats_service import PeriodError, get_stats_payload, resolve_period


def _period_from(request):
    return resolve_period(request.query_params.get('period') or 'month', request.query_params.get('year'))


@api_view(['GET'])
@permission_classes([IsValidated])
def stats_me_view(request):
    try:
        period = _period_from(request)
    except PeriodError as exc:
        return Response({'error': str(exc), 'code': 'invalid_period'}, status=status.HTTP_400_BAD_REQUEST)
    return Response(get_stats_payload(request.user, period))


@api_view(['GET'])
@permission_classes([IsValidated])
def stats_user_view(request, user_id: int):
    target = get_object_or_404(get_user_model(), pk=user_id, is_active=True)
    if not can_view_profile_content(request.user, target):
        return Response(
            {'error': 'Profil non visible.', 'code': 'profile_not_visible'}, status=status.HTTP_403_FORBIDDEN
        )
    try:
        period = _period_from(request)
    except PeriodError as exc:
        return Response({'error': str(exc), 'code': 'invalid_period'}, status=status.HTTP_400_BAD_REQUEST)
    is_self = target.id == request.user.id
    data = get_stats_payload(target, period, public=not is_self)
    if not is_self:
        data['badges'] = earned_badges_public(target)  # {code, label, title, emoji, category, earned_at}
        data['badges_count'] = len(data['badges'])
    return Response({'user_id': target.id, 'username': target.username, **data})


@api_view(['GET'])
@permission_classes([IsValidated])
def badges_me_view(request):
    show_all = str(request.query_params.get('all', '')).lower() in ('1', 'true', 'yes')
    return Response(badges_overview(request.user, next_limit=None if show_all else 5))


@api_view(['GET'])
@permission_classes([IsValidated])
def badge_share_view(request, code: str):
    payload = badge_share_payload(request.user, code)
    if payload is None:  # inconnu, pas obtenu, ou secret pas encore débloqué : même réponse
        return Response({'error': 'Badge non obtenu.', 'code': 'badge_not_earned'}, status=status.HTTP_404_NOT_FOUND)
    return Response(payload)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsValidated])
def weather_city_view(request):
    from .services.weather import CITY_MAX_LENGTH, city_payload, set_city

    if request.method == 'GET':
        return Response(city_payload(request.user))
    if request.method == 'DELETE':
        return Response(set_city(request.user, ''))
    city = request.data.get('city') if isinstance(request.data, dict) else None
    if not isinstance(city, str) or not (2 <= len(city.strip()) <= CITY_MAX_LENGTH):
        return Response(
            {'error': 'Indique le nom de ta ville.', 'code': 'invalid_city'}, status=status.HTTP_400_BAD_REQUEST
        )
    # Chaque changement enfile un géocodage (file Celery, quota Open-Meteo) : plafond quotidien par utilisateur.
    from accounts.services.invite_service import rate_limited

    limit = int(getattr(settings, 'WEATHER_CITY_UPDATES_PER_USER_DAY', 10))
    if rate_limited('weather_city', request.user.id, limit=limit, window_seconds=86400):
        return Response(
            {'error': 'Trop de changements de ville aujourd’hui, réessaie demain.', 'code': 'rate_limited'},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    return Response(set_city(request.user, city))


def _invalid_year(year: int, today):
    if year < 2020 or year > today.year:
        return Response({'error': 'year out of range', 'code': 'invalid_year'}, status=status.HTTP_400_BAD_REQUEST)
    return None


@api_view(['GET'])
@permission_classes([IsValidated])
def recap_status_view(request, year: int):
    today = timezone.localdate()
    if (error := _invalid_year(year, today)) is not None:
        return error
    return Response(recap_status(year, request.user, today=today))


@api_view(['GET'])
@permission_classes([IsValidated])
def recap_view(request, year: int):
    today = timezone.localdate()
    if (error := _invalid_year(year, today)) is not None:
        return error
    if not is_recap_available(year, request.user, today=today):
        return Response(
            {
                'year': year,
                'available': False,
                'available_from': recap_available_from(year).isoformat(),
                'slides': [],
            }
        )
    return Response(get_recap(request.user, year, today=today))
