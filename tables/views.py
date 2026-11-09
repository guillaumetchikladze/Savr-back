"""
API « Les tablées » (contrat front : .claude-work/specs/tables.md).

    GET    /api/tables/                              mes tablées + invitations reçues
    POST   /api/tables/                              {name, emoji?, color?, member_ids?} (invitations)
    GET    /api/tables/{id}/?week=YYYY-MM-DD         semaine collective, membres, rituel du dimanche
    PATCH  /api/tables/{id}/                         {name?, emoji?, color?} (créateur·rice)
    DELETE /api/tables/{id}/                         supprimer (créateur·rice)
    POST   /api/tables/{id}/invite/                  {user_ids: [...]} (ami·es seulement)
    POST   /api/tables/{id}/accept/ | decline/       réponse à une invitation
    POST   /api/tables/{id}/leave/                   quitter
    DELETE /api/tables/{id}/members/{user_id}/       retirer un membre / annuler une invitation (créateur·rice)
    PATCH  /api/tables/{id}/me/                      {share_basket: bool} (rituel « Qui cuisine quoi »)
"""

from __future__ import annotations

from functools import wraps

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated

from . import services
from .services import TableError


def _errors(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except TableError as exc:
            return Response({'error': exc.message, 'code': exc.code}, status=exc.status_code)

    return wrapper


def _week(request):
    from recipes.services.basket_service import BasketError, parse_week

    try:
        return parse_week(request.query_params.get('week'), request.user)
    except BasketError as exc:
        raise TableError(exc.message, 400, 'invalid_week') from exc


def _detail(request, table_id: int, http_status=status.HTTP_200_OK) -> Response:
    data = services.table_detail_payload(request.user, table_id, week_start=_week(request), request=request)
    return Response(data, status=http_status)


@api_view(['GET', 'POST'])
@permission_classes([IsValidated])
@_errors
def tables_view(request):
    if request.method == 'POST':
        data = request.data
        table, results = services.create_table(
            request.user,
            name=data.get('name'),
            emoji=data.get('emoji', ''),
            color=data.get('color', ''),
            member_ids=data.get('member_ids'),
        )
        response = _detail(request, table.id, status.HTTP_201_CREATED)
        response.data['invitations_sent'] = {str(k): v for k, v in results.items()}
        return response
    return Response(services.tables_list_payload(request.user, request))


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsValidated])
@_errors
def table_detail_view(request, table_id: int):
    if request.method == 'PATCH':
        services.update_table(request.user, table_id, request.data)
    elif request.method == 'DELETE':
        services.delete_table(request.user, table_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
    return _detail(request, table_id)


@api_view(['POST'])
@permission_classes([IsValidated])
@_errors
def table_invite_view(request, table_id: int):
    results = services.invite(request.user, table_id, request.data.get('user_ids'))
    response = _detail(request, table_id)
    response.data['invitations_sent'] = {str(k): v for k, v in results.items()}
    return response


@api_view(['POST'])
@permission_classes([IsValidated])
@_errors
def table_accept_view(request, table_id: int):
    services.respond(request.user, table_id, accept=True)
    return _detail(request, table_id)


@api_view(['POST'])
@permission_classes([IsValidated])
@_errors
def table_decline_view(request, table_id: int):
    services.respond(request.user, table_id, accept=False)
    return Response(services.tables_list_payload(request.user, request))


@api_view(['POST'])
@permission_classes([IsValidated])
@_errors
def table_leave_view(request, table_id: int):
    services.leave(request.user, table_id)
    return Response(services.tables_list_payload(request.user, request))


@api_view(['DELETE'])
@permission_classes([IsValidated])
@_errors
def table_member_view(request, table_id: int, user_id: int):
    services.remove_member(request.user, table_id, user_id)
    return _detail(request, table_id)


@api_view(['PATCH'])
@permission_classes([IsValidated])
@_errors
def table_me_view(request, table_id: int):
    services.set_my_settings(request.user, table_id, request.data)
    return _detail(request, table_id)
