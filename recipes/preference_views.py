"""API: mappings communautaires + forks utilisateur pour allergies/goûts."""

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from accounts.permissions import IsValidated as IsAuthenticated
from rest_framework.response import Response

from .models import PreferenceMapping, UserPreferenceMapping


def _norm_kind(kind: str) -> str:
    k = (kind or '').strip().lower()
    return k if k in {'allergy', 'dislike'} else ''


def _norm_label(label: str) -> str:
    return (label or '').strip()


def _norm_keywords(raw):
    if raw is None:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    seen = set()
    for x in raw[:30]:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    return out


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def preference_mappings_list(request):
    """
    Liste des mappings communautaires (pending + validated), filtrables par kind et q.
    Usage: alimenter l'autocomplete + UI "améliorer la détection".
    """
    kind = _norm_kind(request.query_params.get('kind'))
    q = (request.query_params.get('q') or '').strip()

    qs = PreferenceMapping.objects.all()
    if kind:
        qs = qs.filter(kind=kind)
    if q:
        qs = qs.filter(label__icontains=q)
    qs = qs.order_by('kind', 'status', '-usage_count', 'label')[:200]

    data = [
        {
            'id': m.id,
            'kind': m.kind,
            'label': m.label,
            'keywords': m.keywords or [],
            'status': m.status,
            'usage_count': m.usage_count,
            'last_used_at': m.last_used_at,
        }
        for m in qs
    ]
    return Response({'results': data})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def preference_mapping_resolved(request):
    """
    Résout un label pour un user: (override user, mapping global).
    """
    kind = _norm_kind(request.query_params.get('kind'))
    label = _norm_label(request.query_params.get('label'))
    if not kind or not label:
        return Response({'error': 'kind et label requis'}, status=status.HTTP_400_BAD_REQUEST)

    g = PreferenceMapping.objects.filter(kind=kind, label=label).first()
    u = UserPreferenceMapping.objects.filter(user=request.user, kind=kind, label=label).first()

    return Response(
        {
            'kind': kind,
            'label': label,
            'global': None
            if not g
            else {
                'id': g.id,
                'keywords': g.keywords or [],
                'status': g.status,
                'usage_count': g.usage_count,
                'last_used_at': g.last_used_at,
            },
            'override': None
            if not u
            else {
                'id': u.id,
                'keywords': u.keywords or [],
                'base_mapping_id': u.base_mapping_id,
            },
        }
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def preference_mapping_propose(request):
    """
    Proposer un mapping communautaire (toujours pending).
    Ne modifie pas strict, mais peut influencer le scoring global soft.
    """
    kind = _norm_kind(request.data.get('kind'))
    label = _norm_label(request.data.get('label'))
    keywords = _norm_keywords(request.data.get('keywords') or [])
    if not kind or not label:
        return Response({'error': 'kind et label requis'}, status=status.HTTP_400_BAD_REQUEST)

    m, created = PreferenceMapping.objects.get_or_create(
        kind=kind,
        label=label,
        defaults={
            'keywords': keywords,
            'status': 'pending',
            'created_by': request.user,
            'usage_count': 1,
            'last_used_at': timezone.now(),
        },
    )
    if not created:
        # On ne remplace pas automatiquement les keywords (risque). On ne fait qu'incrémenter l'usage.
        PreferenceMapping.objects.filter(pk=m.pk).update(
            usage_count=m.usage_count + 1,
            last_used_at=timezone.now(),
        )

    return Response(
        {
            'id': m.id,
            'kind': m.kind,
            'label': m.label,
            'keywords': m.keywords or [],
            'status': m.status,
        },
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def preference_mapping_fork(request):
    """
    Crée/MAJ un fork user (override). Assure l'existence du mapping global (pending) si absent.
    """
    kind = _norm_kind(request.data.get('kind'))
    label = _norm_label(request.data.get('label'))
    keywords = _norm_keywords(request.data.get('keywords') or [])
    if not kind or not label:
        return Response({'error': 'kind et label requis'}, status=status.HTTP_400_BAD_REQUEST)
    if not keywords:
        return Response({'error': 'keywords requis'}, status=status.HTTP_400_BAD_REQUEST)

    g, _ = PreferenceMapping.objects.get_or_create(
        kind=kind,
        label=label,
        defaults={'keywords': [], 'status': 'pending', 'created_by': request.user},
    )

    u, created = UserPreferenceMapping.objects.update_or_create(
        user=request.user,
        kind=kind,
        label=label,
        defaults={'keywords': keywords, 'base_mapping': g},
    )
    return Response(
        {
            'kind': kind,
            'label': label,
            'override': {'id': u.id, 'keywords': u.keywords or [], 'base_mapping_id': u.base_mapping_id},
            'created': created,
        }
    )


@api_view(['POST'])
@permission_classes([IsAdminUser])
def preference_mapping_validate(request):
    """
    Valider un mapping global pour qu'il impacte le mode strict.
    (Admin only; à remplacer par un workflow plus tard.)
    """
    kind = _norm_kind(request.data.get('kind'))
    label = _norm_label(request.data.get('label'))
    if not kind or not label:
        return Response({'error': 'kind et label requis'}, status=status.HTTP_400_BAD_REQUEST)

    m = PreferenceMapping.objects.filter(kind=kind, label=label).first()
    if not m:
        return Response({'error': 'mapping introuvable'}, status=status.HTTP_404_NOT_FOUND)

    m.status = 'validated'
    m.save(update_fields=['status', 'updated_at'])
    return Response({'id': m.id, 'status': m.status})

