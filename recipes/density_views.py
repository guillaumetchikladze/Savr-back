"""
Onboarding social et « Suggestions » du feed (« Ta bande avant ton premier plat »).
Contrat front : .claude-work/specs/density.md.

- `GET /api/onboarding/social/` : état de la bande (amis, amis actifs, cible 3), premier miam,
  personnes à ajouter, posts d'amis pour un premier miam, lien d'invitation.
- `GET /api/feed/suggestions/` : posts partagés avec la communauté tant qu'il y a moins de 3 amis
  actifs. Endpoint séparé du feed principal (`GET /api/posts/?is_published=true` inchangé : l'app
  1.0.9 ne voit jamais de post « suggéré » qu'elle ne saurait pas afficher).
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.feed_suggestions import build_onboarding_people_suggestions
from accounts.models import InviteRedemption
from accounts.permissions import IsValidated
from accounts.serializers import OnboardingPeopleSuggestionSerializer, UserSerializer
from accounts.services.invite_service import invite_payload

from .pagination import CustomPageNumberPagination
from .serializers import PostListSerializer
from .services.cooked_service import cooked_meal_plans
from .services.density_service import (
    FIRST_MIAM_POSTS,
    TARGET_FRIENDS,
    active_friend_ids,
    community_suggestions_queryset,
    first_miam_posts_queryset,
    friend_ids,
    miams_given_count,
    suggestions_active,
)

SUGGESTIONS_PAGE_SIZE = 10


def _feed_posts(request, posts) -> list[dict]:
    from stats.services.highlights import ensure_post_highlights

    posts = list(posts)
    ensure_post_highlights(posts)
    return PostListSerializer(posts, many=True, context={'request': request}).data


def _density_counts(user) -> tuple[set[int], set[int]]:
    friends = friend_ids(user)
    return friends, active_friend_ids(user, friends)


def _invited_by(request, user) -> dict | None:
    first = (
        InviteRedemption.objects.filter(invitee=user, inviter__is_active=True)
        .select_related('inviter')
        .order_by('created_at')
        .first()
    )
    if not first:
        return None
    inviter = first.inviter
    return {
        'id': inviter.id,
        'username': inviter.username,
        'avatar_url': UserSerializer(context={'request': request}).get_avatar_url(inviter),
    }


@api_view(['GET'])
@permission_classes([IsValidated])
def onboarding_social_view(request):
    from .views import PostViewSet

    user = request.user
    friends, active = _density_counts(user)
    miams = miams_given_count(user)
    people = build_onboarding_people_suggestions(user)
    first_miam = PostViewSet.feed_list_queryset(request, first_miam_posts_queryset(user, friends))
    return Response(
        {
            'friends_count': len(friends),
            'active_friends_count': len(active),
            'target': TARGET_FRIENDS,
            'has_enough_friends': len(friends) >= TARGET_FRIENDS,
            'has_enough_active_friends': len(active) >= TARGET_FRIENDS,
            'feed_suggestions_active': suggestions_active(len(active)),
            'has_given_miam': miams > 0,
            'miams_given_count': miams,
            'has_cooked': cooked_meal_plans(user).exists(),
            'invited_by': _invited_by(request, user),
            'invite': invite_payload(user),
            'suggested_people': OnboardingPeopleSuggestionSerializer(
                people, many=True, context={'request': request}
            ).data,
            'first_miam_posts': _feed_posts(request, first_miam[:FIRST_MIAM_POSTS]),
        }
    )


@api_view(['GET'])
@permission_classes([IsValidated])
def feed_suggestions_view(request):
    """
    Suggestions de la communauté (paginées `?page=`, `page_size` ≤ 20) tant que moins de 3 amis actifs ;
    chaque post porte `suggested: true`. Au-delà : `active: false`, `results: []`.
    """
    from .views import PostViewSet

    user = request.user
    friends, active = _density_counts(user)
    base = {
        'active': suggestions_active(len(active)),
        'friends_count': len(friends),
        'active_friends_count': len(active),
        'target': TARGET_FRIENDS,
    }
    if not base['active']:
        return Response({**base, 'count': 0, 'next': None, 'previous': None, 'results': []})

    queryset = PostViewSet.feed_list_queryset(request, community_suggestions_queryset(user, friends))
    paginator = CustomPageNumberPagination()
    paginator.page_size = SUGGESTIONS_PAGE_SIZE
    paginator.max_page_size = 20
    page = paginator.paginate_queryset(queryset, request)
    results = [{**item, 'suggested': True} for item in _feed_posts(request, page)]
    return Response(
        {
            **base,
            'count': paginator.page.paginator.count,
            'next': paginator.get_next_link(),
            'previous': paginator.get_previous_link(),
            'results': results,
        }
    )
