import logging
import random

from django.contrib.auth import get_user_model

from accounts.models import Notification, PushDevice
from accounts.services.expo_push import send_expo_push_notifications
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
        reposts = (
            PostRepost.objects.filter(original_post=post)
            .select_related('user')
            .order_by('created_at')
        )
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

    titles = [
        'Ton repas fait le tour',
        'Quelqu’un a reposté ton repas',
        'Ton meal plan se propage',
    ]
    messages = [
        f'{reposter.username} a reposté ton repas.',
        f'{reposter.username} partage ton repas avec ses amis.',
        f'{reposter.username} a ajouté ton repas à son historique.',
    ]

    notification = Notification.objects.create(
        user=post.user,
        notification_type='post_repost',
        title=random.choice(titles),
        message=random.choice(messages),
        related_user=reposter,
        related_post_id=post.id,
    )

    devices = PushDevice.objects.filter(
        user=post.user,
        is_active=True,
    ).exclude(expo_push_token='')
    logger.info(
        'post_repost: notification=%s host=%s reposter=%s devices=%s',
        notification.id,
        post.user_id,
        reposter.id,
        devices.count(),
    )
    messages_payload = [
        {
            'to': device.expo_push_token,
            'title': notification.title,
            'body': notification.message,
            'data': {
                'source': 'social',
                'kind': 'post_repost',
                'notification_id': notification.id,
                'post_id': post.id,
            },
            'sound': 'default',
        }
        for device in devices
    ]
    if messages_payload:
        send_expo_push_notifications(messages_payload)
