"""
API des liens d'invitation (`/api/invites/`), cf. .claude-work/specs/density.md.

- `GET  me/`         : mon lien (créé à la demande) + compteurs ;
- `POST me/rotate/`  : nouveau lien (l'ancien ne marche plus), 3 par jour ;
- `POST redeem/`     : utiliser un code après l'inscription (compte en attente compris) ;
- `GET  <code>/`     : aperçu public « Léa t'invite » avant inscription (débit limité par IP).

L'inscription accepte aussi `invite_code` (`POST /api/auth/register/`).
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from core.client_ip import public_rate_limited
from core.public_links import public_display_name

from .permissions import IsValidated
from .serializers import UserSerializer
from .services.invite_service import (
    InviteError,
    invite_payload,
    rate_limited,
    redeem,
    redemption_payload,
    resolve_code,
    rotate_code,
)

HOUR = 3600
DAY = 86400
LOOKUP_LIMIT_PER_IP = 30  # par heure
REDEEM_LIMIT_PER_USER = 10  # par heure
REDEEM_LIMIT_PER_IP = 30  # par heure (inscription comprise)
ROTATE_LIMIT_PER_USER = 3  # par jour


def _too_many():
    return Response(
        {'error': 'Trop de tentatives, réessaie plus tard.', 'code': 'rate_limited'},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


@api_view(['GET'])
@permission_classes([IsValidated])
def my_invite_view(request):
    return Response(invite_payload(request.user))


@api_view(['POST'])
@permission_classes([IsValidated])
def rotate_invite_view(request):
    if rate_limited('rotate', request.user.id, limit=ROTATE_LIMIT_PER_USER, window_seconds=DAY):
        return _too_many()
    rotate_code(request.user)
    return Response(invite_payload(request.user), status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])  # un compte en liste d'attente peut utiliser un code
def redeem_invite_view(request):
    code = request.data.get('code') or request.data.get('invite_code')
    if not code:
        return Response({'error': 'code requis', 'code': 'code_required'}, status=status.HTTP_400_BAD_REQUEST)
    if rate_limited('redeem:user', request.user.id, limit=REDEEM_LIMIT_PER_USER, window_seconds=HOUR):
        return _too_many()
    try:
        result = redeem(request.user, code)
    except InviteError as exc:
        return Response(
            {'error': exc.message, 'code': exc.code, 'invite': redemption_payload(None, exc)},
            status=exc.status_code,
        )
    request.user.refresh_from_db()
    return Response(
        {
            'invite': redemption_payload(result),
            'user': UserSerializer(request.user, context={'request': request}).data,
        },
        status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([AllowAny])
def invite_preview_view(request, code):
    """Aperçu public d'un lien (écran d'inscription) : prénom / avatar du parrain, rien d'autre."""
    if public_rate_limited(request, 'lookup', limit_per_ip=LOOKUP_LIMIT_PER_IP, window_seconds=HOUR):
        return _too_many()
    invite = resolve_code(code)
    if invite is None:
        return Response(
            {'valid': False, 'code': 'invalid_code', 'error': 'Ce lien d’invitation n’est plus valable.'},
            status=status.HTTP_404_NOT_FOUND,
        )
    inviter = invite.user
    avatar = UserSerializer(context={'request': request}).get_avatar_url(inviter)
    return Response(
        {
            'valid': True,
            'code': invite.code,
            'inviter': {
                'id': inviter.id,
                'username': inviter.username,
                'display_name': public_display_name(inviter),
                'avatar_url': avatar,
            },
        }
    )
