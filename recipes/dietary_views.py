"""Vues API liées au filtrage alimentaire (conflits recettes / préférences)."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from accounts.permissions import IsValidated as IsAuthenticated
from rest_framework.response import Response

from accounts.privacy import are_complices_in_network

from .dietary_filters import (
    conflicting_recipe_ids_for_user,
    conflict_reasons_by_recipe_id,
    hits_for_recipe_reasons,
)
from .models import Recipe

User = get_user_model()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def dietary_conflicts_view(request):
    """
    Recettes parmi ``recipe_ids`` qui poseraient problème pour ``target_user_id``
    selon allergies / goûts (même moteur que les listes filtrées).

    L'appelant doit être en relation complice (au moins un sens de suivi)
    avec la cible, comme pour les invitations repas.
    """
    raw_ids = request.data.get('recipe_ids') or []
    target_id = request.data.get('target_user_id')
    if target_id is None:
        return Response({'error': 'target_user_id requis'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        return Response({'error': 'target_user_id invalide'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        target_user = User.objects.get(pk=target_id)
    except User.DoesNotExist:
        return Response({'error': 'Utilisateur introuvable'}, status=status.HTTP_404_NOT_FOUND)

    if not are_complices_in_network(request.user, target_user):
        return Response({'error': 'Non autorisé'}, status=status.HTTP_403_FORBIDDEN)

    parsed = []
    for x in raw_ids:
        try:
            parsed.append(int(x))
        except (TypeError, ValueError):
            continue
    parsed = list(dict.fromkeys(parsed))

    conflict_ids = conflicting_recipe_ids_for_user(parsed, target_user)
    reasons_map = conflict_reasons_by_recipe_id(parsed, target_user)
    conflicts = []
    if conflict_ids:
        for r in Recipe.objects.filter(id__in=conflict_ids).values('id', 'title'):
            rid = r['id']
            reasons = reasons_map.get(rid) or []
            hits = hits_for_recipe_reasons(rid, reasons, target_user)
            conflicts.append(
                {
                    'recipe_id': rid,
                    'title': r['title'] or '',
                    'reasons': reasons,
                    'hits': hits,
                }
            )

    return Response({'conflicts': conflicts})
