import logging

from django.contrib.auth import get_user_model

from notifications.services import notify
from recipes.models import MealInvitation, Post, PostRepost

User = get_user_model()
logger = logging.getLogger(__name__)


def user_can_repost_post(user, post: Post) -> bool:
    if not user or not user.is_authenticated:
        return False
    if not post.is_published or not post.meal_plan_id:
        return False
    if post.user_id == user.id:
        return False
    if PostRepost.objects.filter(user=user, original_post=post).exists():
        return False
    return MealInvitation.objects.filter(
        invitee=user,
        meal_plan_id=post.meal_plan_id,
        status__in=['pending', 'accepted'],
    ).exists()


def get_co_cooked_users(post: Post):
    """Hôte + reposteurs, ordre stable, sans doublon."""
    users = []
    seen = set()
    if post.user_id and post.user_id not in seen:
        seen.add(post.user_id)
        users.append(post.user)
    reposts = getattr(post, '_prefetched_objects_cache', {}).get('reposts')
    if reposts is None:
        reposts = PostRepost.objects.filter(original_post=post).select_related('user').order_by('created_at')
    for repost in reposts:
        repost_user = repost.user
        if repost_user.id not in seen:
            seen.add(repost_user.id)
            users.append(repost_user)
    return users


def create_post_repost(user, post: Post) -> PostRepost:
    repost, created = PostRepost.objects.get_or_create(
        user=user,
        original_post=post,
        defaults={'host_user_id': post.user_id},
    )
    if created:
        notify_host_of_repost(user, post)
    return repost


def delete_post_repost(user, post: Post) -> bool:
    deleted, _ = PostRepost.objects.filter(user=user, original_post=post).delete()
    return deleted > 0


def notify_host_of_repost(reposter, post: Post) -> None:
    if post.user_id == reposter.id:
        return
    notify('post_repost', post.user, actor=reposter, post=post)
