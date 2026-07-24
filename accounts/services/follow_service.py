"""Logique métier des demandes de suivi et relations complice."""
from __future__ import annotations

import random
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from ..models import Follow, FollowRequest, Notification, PushDevice
from .expo_push import send_expo_push_notifications

User = get_user_model()


def get_follow_relation(viewer: Optional[User], target: User) -> dict:
    """État de la relation entre viewer et target."""
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return {
            'is_following': False,
            'is_followed_by': False,
            'follow_request_outgoing': False,
            'follow_request_incoming': False,
        }
    if viewer.id == target.id:
        return {
            'is_following': False,
            'is_followed_by': False,
            'follow_request_outgoing': False,
            'follow_request_incoming': False,
        }
    return {
        'is_following': Follow.objects.filter(follower=viewer, following=target).exists(),
        'is_followed_by': Follow.objects.filter(follower=target, following=viewer).exists(),
        'follow_request_outgoing': FollowRequest.objects.filter(
            requester=viewer, target=target, status='pending'
        ).exists(),
        'follow_request_incoming': FollowRequest.objects.filter(
            requester=target, target=viewer, status='pending'
        ).exists(),
    }


def follow_relation_payload(viewer: Optional[User], target: User) -> dict:
    rel = get_follow_relation(viewer, target)
    return {
        **rel,
        'is_mutual': rel['is_following'] and rel['is_followed_by'],
    }


def _push_to_user(user: User, *, title: str, message: str, kind: str, related_user_id: int, notification_id: int):
    devices = PushDevice.objects.filter(user=user, is_active=True).exclude(expo_push_token='')
    messages = [
        {
            'to': device.expo_push_token,
            'title': title,
            'body': message,
            'data': {
                'source': 'social',
                'kind': kind,
                'notification_id': notification_id,
                'user_id': related_user_id,
            },
            'sound': 'default',
        }
        for device in devices
    ]
    if messages:
        send_expo_push_notifications(messages)


def _create_notification(
    *,
    user: User,
    notification_type: str,
    title: str,
    message: str,
    related_user: User,
    push_kind: str,
):
    notification = Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        message=message,
        related_user=related_user,
    )
    _push_to_user(
        user,
        title=title,
        message=message,
        kind=push_kind,
        related_user_id=related_user.id,
        notification_id=notification.id,
    )
    return notification


def _notify_follow_request(requester: User, target: User):
    titles = [
        'Nouvelle demande d\'ami',
        'Quelqu\'un veut te rejoindre',
    ]
    messages = [
        f'{requester.username} souhaite devenir ton complice.',
        f'{requester.username} t\'a envoyé une demande d\'ami.',
    ]
    _create_notification(
        user=target,
        notification_type='follow_request',
        title=random.choice(titles),
        message=random.choice(messages),
        related_user=requester,
        push_kind='follow_request',
    )


def _notify_new_follower(follower: User, following: User):
    titles = [
        'Un nouvel ami arrive',
        'Tu as gagné un nouvel allié',
    ]
    messages = [
        f'{follower.username} t\'a ajouté comme ami.',
        f'{follower.username} a commencé à te suivre.',
    ]
    _create_notification(
        user=following,
        notification_type='follow',
        title=random.choice(titles),
        message=random.choice(messages),
        related_user=follower,
        push_kind='follow',
    )


def _notify_request_accepted(accepter: User, requester: User):
    titles = [
        'Demande acceptée',
        'C\'est validé',
    ]
    messages = [
        f'{accepter.username} a accepté ta demande d\'ami.',
        f'{accepter.username} et toi êtes maintenant connectés.',
    ]
    _create_notification(
        user=requester,
        notification_type='follow',
        title=random.choice(titles),
        message=random.choice(messages),
        related_user=accepter,
        push_kind='follow',
    )


def _create_follow(follower: User, following: User, *, notify: bool = True) -> tuple[Follow, bool]:
    follow, created = Follow.objects.get_or_create(follower=follower, following=following)
    if created and notify:
        _notify_new_follower(follower, following)
    return follow, created


def _resolve_pending_request(requester: User, target: User, *, new_status: str):
    FollowRequest.objects.filter(
        requester=requester,
        target=target,
        status='pending',
    ).update(status=new_status)


def _clear_follow_requests(user_a: User, user_b: User):
    """Supprime les demandes entre deux utilisateurs (plus de pending fantôme)."""
    FollowRequest.objects.filter(
        Q(requester=user_a, target=user_b) | Q(requester=user_b, target=user_a)
    ).delete()


def _accept_request(request: FollowRequest, *, notify_requester: bool = True) -> Follow:
    follow, _ = _create_follow(request.requester, request.target, notify=False)
    request.status = 'accepted'
    request.save(update_fields=['status', 'updated_at'])
    if notify_requester:
        _notify_request_accepted(request.target, request.requester)
    return follow


def _establish_mutual(requester: User, target: User, incoming_request: Optional[FollowRequest] = None):
    """Accepte la demande entrante et crée le suivi retour sans nouvelle validation."""
    if incoming_request:
        _accept_request(incoming_request, notify_requester=True)
    elif FollowRequest.objects.filter(
        requester=target, target=requester, status='pending'
    ).exists():
        req = FollowRequest.objects.select_for_update().get(
            requester=target, target=requester, status='pending'
        )
        _accept_request(req, notify_requester=True)

    _create_follow(requester, target, notify=False)
    _resolve_pending_request(requester, target, new_status='accepted')
    _resolve_pending_request(target, requester, new_status='accepted')


def _ensure_follow_request(requester: User, target: User) -> tuple[FollowRequest, bool]:
    """
    Crée ou réactive une demande en attente.
    Retourne (request, created_or_reactivated).
    """
    existing = FollowRequest.objects.filter(requester=requester, target=target).first()
    if existing:
        if existing.status == 'pending':
            return existing, False
        existing.status = 'pending'
        existing.save(update_fields=['status', 'updated_at'])
        return existing, True
    return FollowRequest.objects.create(requester=requester, target=target, status='pending'), True


@transaction.atomic
def request_follow(requester: User, target: User) -> dict:
    rel = get_follow_relation(requester, target)

    if rel['is_following']:
        return {
            'action': 'already_following',
            'message': 'Vous suivez déjà cet utilisateur',
            **follow_relation_payload(requester, target),
        }

    if rel['is_followed_by']:
        _create_follow(requester, target, notify=True)
        _resolve_pending_request(requester, target, new_status='accepted')
        _resolve_pending_request(target, requester, new_status='accepted')
        return {
            'action': 'followed_auto',
            'message': 'Abonnement confirmé',
            **follow_relation_payload(requester, target),
        }

    incoming = (
        FollowRequest.objects.select_for_update()
        .filter(requester=target, target=requester, status='pending')
        .first()
    )
    if incoming:
        _establish_mutual(requester, target, incoming)
        return {
            'action': 'mutual_established',
            'message': 'Vous êtes maintenant complices',
            **follow_relation_payload(requester, target),
        }

    if rel['follow_request_outgoing']:
        return {
            'action': 'already_pending',
            'message': 'Demande déjà envoyée',
            **follow_relation_payload(requester, target),
        }

    _, reactivated = _ensure_follow_request(requester, target)
    if reactivated:
        _notify_follow_request(requester, target)
    return {
        'action': 'request_sent',
        'message': 'Demande envoyée',
        **follow_relation_payload(requester, target),
    }


@transaction.atomic
def accept_follow_request(accepter: User, requester: User) -> dict:
    rel = get_follow_relation(accepter, requester)
    if rel['is_followed_by']:
        _resolve_pending_request(requester, accepter, new_status='accepted')
        return {
            'action': 'already_following',
            'message': 'Cet utilisateur vous suit déjà',
            **follow_relation_payload(accepter, requester),
        }

    try:
        follow_request = FollowRequest.objects.select_for_update().get(
            requester=requester,
            target=accepter,
            status='pending',
        )
    except FollowRequest.DoesNotExist:
        return {
            'action': 'not_found',
            'message': 'Aucune demande en attente',
            **follow_relation_payload(accepter, requester),
        }

    _accept_request(follow_request, notify_requester=True)
    return {
        'action': 'accepted',
        'message': 'Demande acceptée',
        **follow_relation_payload(accepter, requester),
    }


@transaction.atomic
def decline_follow_request(decliner: User, requester: User) -> dict:
    pending = list(
        FollowRequest.objects.select_for_update().filter(
            requester=requester,
            target=decliner,
            status='pending',
        )
    )
    if not pending:
        return {
            'action': 'not_found',
            'message': 'Aucune demande en attente',
            **follow_relation_payload(decliner, requester),
        }
    for follow_request in pending:
        follow_request.status = 'declined'
        follow_request.save(update_fields=['status', 'updated_at'])
    return {
        'action': 'declined',
        'message': 'Demande refusée',
        **follow_relation_payload(decliner, requester),
    }


def _revoke_follower_access(accepter: User, follower: User) -> bool:
    """
    Retire l'accès d'un abonné : supprime son Follow et sa demande associée.
    Il devra renvoyer une demande pour retrouver l'accès.
    """
    removed, _ = Follow.objects.filter(follower=follower, following=accepter).delete()
    if removed:
        FollowRequest.objects.filter(requester=follower, target=accepter).delete()
    return removed > 0


@transaction.atomic
def unfollow_or_cancel(requester: User, target: User) -> dict:
    rel = get_follow_relation(requester, target)

    if rel['follow_request_outgoing'] and not rel['is_following'] and not rel['is_followed_by']:
        FollowRequest.objects.filter(
            requester=requester,
            target=target,
            status='pending',
        ).delete()
        return {
            'action': 'cancelled',
            'message': 'Demande annulée',
            **follow_relation_payload(requester, target),
        }

    # Ils me suivent sans que je les suive → retirer leur accès
    if rel['is_followed_by'] and not rel['is_following']:
        if _revoke_follower_access(requester, target):
            return {
                'action': 'access_revoked',
                'message': 'Accès retiré',
                **follow_relation_payload(requester, target),
            }
        return {
            'action': 'nothing',
            'message': 'Aucune relation à supprimer',
            **follow_relation_payload(requester, target),
        }

    # Je les suis seul → je me désabonne
    if rel['is_following'] and not rel['is_followed_by']:
        Follow.objects.filter(follower=requester, following=target).delete()
        FollowRequest.objects.filter(requester=requester, target=target).delete()
        return {
            'action': 'unfollowed',
            'message': 'Vous ne suivez plus cet utilisateur',
            **follow_relation_payload(requester, target),
        }

    # Amis mutuels → tout couper, sans rouvrir de demandes fantômes
    if rel['is_following'] and rel['is_followed_by']:
        Follow.objects.filter(
            Q(follower=requester, following=target) | Q(follower=target, following=requester)
        ).delete()
        _clear_follow_requests(requester, target)
        return {
            'action': 'disconnected',
            'message': 'Connexion supprimée',
            **follow_relation_payload(requester, target),
        }

    return {
        'action': 'nothing',
        'message': 'Aucune relation à supprimer',
        **follow_relation_payload(requester, target),
    }
