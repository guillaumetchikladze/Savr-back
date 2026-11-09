"""
« Ta cuisine » : équipements de l'utilisateur + « Adapter pour chez moi » (contrat front : .claude-work/specs/equipment.md).

    GET  /api/equipment/                       cuisine effective (la mienne, sinon celle du foyer, sinon le classique)
    PUT  /api/equipment/                       {equipment: [{key, burners?, induction?, count?}]} | {keys: [...], burners?, induction?, ovens?}
    GET  /api/equipment/catalog/               catalogue (clés, libellés, icônes Ionicons, durs / mous, alternatives)
    POST /api/recipes/<id>/adapt-equipment/    copie privée adaptée (201 créée, 200 existante) ; LLM synchrone ≤ ~8 s
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated

from .services import equipment_catalog as catalog
from .services import equipment_service as equipment
from .services.equipment_adaptation import AdaptationError, adapt_recipe_for_user, adaptation_payload


@api_view(['GET', 'PUT'])
@permission_classes([IsValidated])
def equipment_view(request):
    if request.method == 'PUT':
        try:
            return Response(equipment.set_equipment(request.user, request.data))
        except catalog.EquipmentError as exc:
            return Response({'error': exc.message, 'code': exc.code}, status=status.HTTP_400_BAD_REQUEST)
    return Response(equipment.equipment_payload(request.user))


@api_view(['GET'])
@permission_classes([IsValidated])
def equipment_catalog_view(request):
    return Response(catalog.catalog_payload())


@api_view(['POST'])
@permission_classes([IsValidated])
def recipe_adapt_equipment_view(request, recipe_id: int):
    try:
        copy, created, missing = adapt_recipe_for_user(request.user, recipe_id)
    except AdaptationError as exc:
        return Response({'error': exc.message, 'code': exc.code}, status=exc.status_code)
    return Response(
        adaptation_payload(copy, created, missing, {'request': request}),
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )
