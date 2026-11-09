"""
Densité sociale (« Tes amis avant ton premier plat », .claude-work/specs/density.md).

- **Ami** = compte que l'utilisateur suit (Follow approuvé, mutuel ou non), hors blocages :
  c'est ce qui remplit son feed.
- **Ami actif** = ami qui a cuisiné (`cooked_meal_plans_of_users`, `MealPlan.confirmed`) dans les
  `ACTIVE_WINDOW_DAYS` derniers jours.
- Tant qu'il y a moins de `TARGET_FRIENDS` amis actifs, l'app affiche des « Suggestions » : posts
  publiés **et partagés avec la communauté** (`Post.is_public`, choix explicite de l'auteur), jamais
  ceux réservés aux amis, jamais d'un compte bloqué, jamais de critère de lieu.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import QuerySet
from django.utils import timezone

from accounts.blocks import blocked_user_ids_for
from accounts.models import Follow

from ..models import Post, PostCookie
from .cooked_service import cooked_meal_plans_of_users

TARGET_FRIENDS = 3
ACTIVE_WINDOW_DAYS = 14
SUGGESTIONS_MAX_AGE_DAYS = 30
FIRST_MIAM_POSTS = 3
FIRST_MIAM_MAX_AGE_DAYS = 30


def friend_ids(user: AbstractBaseUser) -> set[int]:
    following = set(Follow.objects.filter(follower_id=user.id).values_list('following_id', flat=True))
    return following - blocked_user_ids_for(user) - {user.id}


def active_friend_ids(user: AbstractBaseUser, friends: set[int] | None = None) -> set[int]:
    friends = friend_ids(user) if friends is None else friends
    if not friends:
        return set()
    start = timezone.localdate() - timedelta(days=ACTIVE_WINDOW_DAYS)
    return set(cooked_meal_plans_of_users(friends, start=start).values_list('user_id', flat=True).distinct())


def miams_given_count(user: AbstractBaseUser) -> int:
    return PostCookie.objects.filter(user_id=user.id).exclude(post__user_id=user.id).count()


def suggestions_active(active_count: int) -> bool:
    return active_count < TARGET_FRIENDS


def community_suggestions_queryset(user: AbstractBaseUser, friends: set[int] | None = None) -> QuerySet[Post]:
    """Posts récents partagés avec la communauté, hors amis (déjà dans le feed), soi et blocages."""
    friends = friend_ids(user) if friends is None else friends
    excluded = friends | {user.id} | blocked_user_ids_for(user)
    since = timezone.now() - timedelta(days=SUGGESTIONS_MAX_AGE_DAYS)
    return Post.objects.filter(
        is_published=True,
        is_public=True,
        created_at__gte=since,
        user__is_active=True,
    ).exclude(user_id__in=excluded)


def first_miam_posts_queryset(user: AbstractBaseUser, friends: set[int] | None = None) -> QuerySet[Post]:
    """Posts récents d'amis sans miam de l'utilisateur (« donne ton premier miam »)."""
    friends = friend_ids(user) if friends is None else friends
    since = timezone.now() - timedelta(days=FIRST_MIAM_MAX_AGE_DAYS)
    return (
        Post.objects.filter(is_published=True, user_id__in=friends, created_at__gte=since)
        .exclude(cookies__user_id=user.id)
        .order_by('-created_at')
    )
