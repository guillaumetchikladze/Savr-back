"""
Endpoints stats / badges / Récap.

GET /api/stats/me/?period=week|month|year|all&year=2026
GET /api/stats/users/{id}/?period=…  (sous-ensemble public + badges obtenus, mêmes règles que le profil)
GET /api/badges/me/[?all=1]  (all=1 : tous les badges verrouillés avec progression dans `next`)
GET /api/recap/{year}/        (mis en cache, cf. recap.get_recap)
GET /api/recap/{year}/status/ (disponibilité seule, sans construire le Récap)
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated
from accounts.privacy import can_view_profile_content

from .services.badges import badges_overview, earned_badges_public
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
