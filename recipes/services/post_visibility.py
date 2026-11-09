"""
Visibilité d'un post : **source unique** pour toutes les actions ciblant un post (`/api/posts/{id}/…`).

Deux niveaux :

- ``VIEW`` (ouvrir le détail, miam + liste des miams, « À mon tour », signaler, reposter) : post publié,
  auteur actif, pas de blocage dans un sens ou l'autre avec l'auteur, et au moins un de :
  c'est le sien ; le viewer suit l'auteur (Follow approuvé) ; il est invité (en attente / accepté) au repas
  du post ou l'a reposté (co-cuisiné) ; le post est partagé avec la communauté (`is_public`) ; un compte
  qu'il suit l'a reposté (déjà listé sur le profil de ce compte).
- ``FRIENDS`` (commentaires : lecture, écriture, likes) : mêmes conditions sans `is_public` ni « reposté par
  un ami » : soi, suivi de l'auteur, invité au repas ou co-cuisiné. Un post `is_public` ne s'ouvre pas aux
  commentaires des inconnus (décision densité : miam + « À mon tour » seulement).

Un post invisible renvoie **404** (jamais 403) pour ne pas révéler son existence.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet
from django.shortcuts import get_object_or_404

from recipes.models import MealInvitation, Post, PostRepost

VIEW = 'view'
FRIENDS = 'friends'


def _followed_ids(user) -> QuerySet:
    from accounts.models import Follow

    return Follow.objects.filter(follower_id=user.id).values('following_id')


def visible_posts(user, *, level: str = VIEW, queryset: QuerySet | None = None) -> QuerySet:
    """Posts que `user` peut voir (`VIEW`) ou commenter (`FRIENDS`) ; `queryset` permet de garder ses préchargements."""
    from accounts.blocks import blocked_user_ids_for

    qs = Post.objects.all() if queryset is None else queryset
    if not user or not getattr(user, 'is_authenticated', False):
        return qs.none()
    qs = qs.filter(is_published=True, user__is_active=True)

    followed = _followed_ids(user)
    my_reposts = PostRepost.objects.filter(user_id=user.id).values('original_post_id')
    my_meals = MealInvitation.objects.filter(invitee_id=user.id, status__in=('pending', 'accepted')).values(
        'meal_plan_id'
    )
    allowed = Q(user_id=user.id) | Q(user_id__in=followed) | Q(id__in=my_reposts) | Q(meal_plan_id__in=my_meals)
    if level == VIEW:
        friends_reposts = PostRepost.objects.filter(user_id__in=followed).values('original_post_id')
        allowed |= Q(is_public=True) | Q(id__in=friends_reposts)
    elif level != FRIENDS:
        raise ValueError(f'Unknown post visibility level: {level}')

    qs = qs.filter(allowed)
    blocked = blocked_user_ids_for(user)
    if blocked:
        qs = qs.exclude(user_id__in=blocked)
    return qs


def can_see_post(user, post: Post, *, level: str = VIEW) -> bool:
    if post is None or not post.is_published:
        return False
    return visible_posts(user, level=level).filter(pk=post.pk).exists()


def get_visible_post_or_404(user, pk, *, level: str = VIEW, queryset: QuerySet | None = None) -> Post:
    """Post visible par `user` (sinon `Http404`, même réponse qu'un id inexistant)."""
    return get_object_or_404(visible_posts(user, level=level, queryset=queryset), pk=pk)


def request_blocked_user_ids(request) -> set[int]:
    """Blocages du viewer, calculés une fois par requête (sérialisation de listes sans N+1)."""
    from accounts.blocks import blocked_user_ids_for

    if request is None:
        return set()
    cached = getattr(request, '_post_visibility_blocked_ids', None)
    if cached is None:
        user = getattr(request, 'user', None)
        cached = blocked_user_ids_for(user) if user and getattr(user, 'is_authenticated', False) else set()
        try:
            request._post_visibility_blocked_ids = cached
        except AttributeError:
            pass
    return cached
