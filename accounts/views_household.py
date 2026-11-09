"""
API du foyer (« Foyer + invités »). Contrat front : `.claude-work/specs/household.md`.

    GET    /api/household/                               membres + demandes reçues
    PUT    /api/household/                               {members: [{id?, user_id?, name?, is_child?}]}
    POST   /api/household/members/                       {user_id} (demande) | {name, is_child?}
    PATCH  /api/household/members/{id}/                  {name?, is_child?} (mes entrées nominatives)
    DELETE /api/household/members/{id}/                  retirer / quitter / annuler une demande
    POST   /api/household/requests/{id}/accept/
    POST   /api/household/requests/{id}/decline/
"""

from __future__ import annotations

from functools import wraps

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from .permissions import IsValidated
from .services import household_service as household
from .services.household_service import HouseholdError


def _household_errors(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except HouseholdError as exc:
            return Response({'error': exc.message, 'code': exc.code}, status=exc.status_code)

    return wrapper


def _payload(request) -> dict:
    return household.household_payload(request.user, {'request': request})


@api_view(['GET', 'PUT'])
@permission_classes([IsValidated])
@_household_errors
def household_view(request):
    if request.method == 'PUT':
        household.replace_household(request.user, request.data.get('members'))
    return Response(_payload(request))


@api_view(['POST'])
@permission_classes([IsValidated])
@_household_errors
def household_members_view(request):
    data = request.data
    if data.get('user_id') is not None:
        _, action = household.request_member(request.user, data.get('user_id'))
    else:
        household.add_named_member(request.user, name=data.get('name'), is_child=data.get('is_child', False))
        action = 'added'
    created = action in ('request_sent', 'accepted', 'added')
    return Response(
        {'action': action, **_payload(request)}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsValidated])
@_household_errors
def household_member_detail_view(request, entry_id: int):
    if request.method == 'DELETE':
        household.remove_entry(request.user, entry_id)
    else:
        household.update_named_member(
            request.user, entry_id, name=request.data.get('name'), is_child=request.data.get('is_child')
        )
    return Response(_payload(request))


@api_view(['POST'])
@permission_classes([IsValidated])
@_household_errors
def household_request_accept_view(request, entry_id: int):
    household.respond(request.user, entry_id, accept=True)
    return Response(_payload(request))


@api_view(['POST'])
@permission_classes([IsValidated])
@_household_errors
def household_request_decline_view(request, entry_id: int):
    household.respond(request.user, entry_id, accept=False)
    return Response(_payload(request))
