"""Vues API liées au filtrage alimentaire (conflits recettes / préférences, exceptions)."""

from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated as IsAuthenticated
from accounts.privacy import are_complices_in_network

from .dietary_filters import PREFERENCE_KINDS, conflict_details_by_recipe_id, declared_labels_for_kind
from .dietary_matching import match_key
from .models import Ingredient, PreferenceIngredientException, Recipe

User = get_user_model()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def dietary_conflicts_view(request):
    """
    Recettes parmi ``recipe_ids`` qui poseraient problème pour ``target_user_id``
    selon allergies / régimes / goûts (même moteur que les listes filtrées).

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

    details = conflict_details_by_recipe_id(raw_ids, target_user, viewer=request.user)
    conflicts = []
    if details:
        for r in Recipe.objects.filter(id__in=list(details)).values('id', 'title'):
            d = details[r['id']]
            conflicts.append(
                {
                    'recipe_id': r['id'],
                    'title': r['title'] or '',
                    'reasons': d['reasons'],
                    'hits': d['hits'],
                }
            )

    return Response({'conflicts': conflicts})


def _serialize_exception(exc):
    return {
        'id': exc.id,
        'target_user_id': exc.user_id,
        'kind': exc.kind,
        'label': exc.label,
        'ingredient_id': exc.ingredient_id,
        'ingredient': exc.ingredient.name if exc.ingredient_id else None,
        'created_by_id': exc.created_by_id,
        # Posée par la personne elle-même → vaut partout ; par un hôte → seulement ses alertes.
        'applies_to': 'all' if exc.created_by_id == exc.user_id else 'creator',
        'created_at': exc.created_at,
    }


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def dietary_conflict_dismiss_view(request):
    """
    « Pas un problème » : ne plus signaler ``ingredient_id`` pour la préférence ``label``
    (``kind`` = allergy | diet | dislike) de ``target_user_id`` (défaut : soi-même).

    - Sur ses propres préférences : tous les types, l'exception vaut partout.
    - Pour un complice (hôte → invité) : goûts (``dislike``) uniquement — une allergie ou un
      régime ne peut être levé que par la personne concernée — et l'exception ne vaut que
      dans les alertes vues par cet hôte.
    """
    me = request.user
    raw_target = request.data.get('target_user_id', request.data.get('user_id'))
    kind = (request.data.get('kind') or '').strip().lower()
    label = (request.data.get('label') or '').strip()
    ingredient_id = request.data.get('ingredient_id')

    if kind not in PREFERENCE_KINDS:
        return Response({'error': 'kind invalide (allergy, diet, dislike)'}, status=status.HTTP_400_BAD_REQUEST)
    if not label:
        return Response({'error': 'label requis'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        ingredient_id = int(ingredient_id)
        target_id = int(raw_target) if raw_target not in (None, '') else me.id
    except (TypeError, ValueError):
        return Response({'error': 'ingredient_id / target_user_id invalide'}, status=status.HTTP_400_BAD_REQUEST)

    target = me if target_id == me.id else User.objects.filter(pk=target_id).first()
    if target is None:
        return Response({'error': 'Utilisateur introuvable'}, status=status.HTTP_404_NOT_FOUND)
    if target.id != me.id:
        if not are_complices_in_network(me, target):
            return Response({'error': 'Non autorisé'}, status=status.HTTP_403_FORBIDDEN)
        if kind != 'dislike':
            return Response(
                {'error': 'Seule la personne concernée peut lever une alerte allergie ou régime'},
                status=status.HTTP_403_FORBIDDEN,
            )

    ingredient = Ingredient.objects.filter(pk=ingredient_id).first()
    if ingredient is None:
        return Response({'error': 'Ingrédient introuvable'}, status=status.HTTP_404_NOT_FOUND)

    key = match_key(label)
    declared = next((lbl for lbl in declared_labels_for_kind(target, kind) if match_key(lbl) == key), None)
    if declared is None:
        return Response({'error': 'Préférence non déclarée'}, status=status.HTTP_400_BAD_REQUEST)

    exc, created = PreferenceIngredientException.objects.get_or_create(
        user=target, created_by=me, kind=kind, label=declared, ingredient=ingredient
    )
    return Response(
        _serialize_exception(exc),
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dietary_exceptions_list_view(request):
    """Exceptions sur mes préférences (quel qu'en soit l'auteur) + celles que j'ai posées."""
    me = request.user
    qs = (
        PreferenceIngredientException.objects.filter(user__isnull=False)
        .filter(Q(user=me) | Q(created_by=me))
        .select_related('ingredient')
        .order_by('-created_at', '-id')
    )
    return Response({'exceptions': [_serialize_exception(e) for e in qs]})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def dietary_exception_delete_view(request, pk):
    """Supprime une exception : la personne concernée ou son auteur (règles globales exclues)."""
    me = request.user
    exc = (
        PreferenceIngredientException.objects.filter(pk=pk, user__isnull=False)
        .filter(Q(user=me) | Q(created_by=me))
        .first()
    )
    if exc is None:
        return Response({'error': 'Introuvable'}, status=status.HTTP_404_NOT_FOUND)
    exc.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
