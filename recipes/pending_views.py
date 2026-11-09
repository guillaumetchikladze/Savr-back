"""
« Je l'ai déjà » (contrat front : .claude-work/specs/tables.md, partie « Je l'ai déjà »).

    GET    /api/cooked/pending-confirmations/[?preview=1]                     ≤ 3 repas passés à confirmer
    POST   /api/cooked/pending-confirmations/{kind}/{id}/dismiss/             « Pas fait » (kind : meal_plan | basket_item)
    DELETE /api/cooked/pending-confirmations/{kind}/{id}/dismiss/             annuler le « Pas fait »

« Oui » = `POST /api/posts/cooked/` avec `cooked_payload` de l'élément (le repas sort de la liste).
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated

from .services import pending_confirmations as pending


def _truthy(value) -> bool:
    return str(value or '').strip().lower() in ('1', 'true', 'yes')


@api_view(['GET'])
@permission_classes([IsValidated])
def pending_confirmations_view(request):
    return Response(pending.pending_payload(request.user, with_preview=_truthy(request.query_params.get('preview'))))


@api_view(['POST', 'DELETE'])
@permission_classes([IsValidated])
def pending_confirmation_dismiss_view(request, kind: str, object_id: int):
    try:
        if request.method == 'DELETE':
            pending.undo_dismiss(request.user, kind, object_id)
            dismissed = False
        else:
            pending.dismiss(request.user, kind, object_id)
            dismissed = True
    except pending.PendingError as exc:
        return Response({'error': exc.message, 'code': exc.code}, status=exc.status_code)
    return Response({'dismissed': dismissed, 'kind': kind, 'id': object_id, **pending.pending_payload(request.user)})
