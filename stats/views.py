"""
Endpoints stats / badges / Récap.

GET /api/stats/me/?period=week|month|year|all&year=2026
GET /api/stats/users/{id}/?period=…  (sous-ensemble public, mêmes règles que le profil : suivre la personne)
GET /api/badges/me/
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated
from accounts.privacy import can_view_profile_content

from .models import UserBadge
from .services.badges import badges_overview
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
    target = get_object_or_404(get_user_model(), pk=user_id)
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
        data['badges_count'] = UserBadge.objects.filter(user=target).count()
    return Response({'user_id': target.id, 'username': target.username, **data})


@api_view(['GET'])
@permission_classes([IsValidated])
def badges_me_view(request):
    return Response(badges_overview(request.user))
