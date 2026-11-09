"""Logique métier des demandes de suivi et relations d'ami."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from notifications.services import notify

from ..models import Follow, FollowRequest

User = get_user_model()


def get_follow_relation(viewer: User | None, target: User) -> dict:
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


def get_follow_relations(viewer: User | None, target_ids) -> dict[int, dict]:
    """
    `get_follow_relation` en lot : {target_id: relation} pour plusieurs comptes, en 2 requêtes au plus
    (listes de posts : auteurs + co-cuisiniers). Mêmes clés, même ordre, mêmes valeurs.
    """
    ids = {int(i) for i in target_ids if i is not None}
    relations = {
        i: {
            'is_following': False,
            'is_followed_by': False,
            'follow_request_outgoing': False,
            'follow_request_incoming': False,
        }
        for i in ids
    }
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return relations
    others = ids - {viewer.id}
    if not others:
        return relations
    for follower_id, following_id in Follow.objects.filter(
        Q(follower_id=viewer.id, following_id__in=others) | Q(follower_id__in=others, following_id=viewer.id)
    ).values_list('follower_id', 'following_id'):
        if follower_id == viewer.id:
            relations[following_id]['is_following'] = True
        else:
            relations[follower_id]['is_followed_by'] = True
    for requester_id, target_id in FollowRequest.objects.filter(
        Q(requester_id=viewer.id, target_id__in=others) | Q(requester_id__in=others, target_id=viewer.id),
        status='pending',
    ).values_list('requester_id', 'target_id'):
        if requester_id == viewer.id:
            relations[target_id]['follow_request_outgoing'] = True
        else:
            relations[requester_id]['follow_request_incoming'] = True
    return relations


def follow_relation_payload(viewer: User | None, target: User) -> dict:
    rel = get_follow_relation(viewer, target)
    return {
        **rel,
        'is_mutual': rel['is_following'] and rel['is_followed_by'],
    }


def _notify_follow_request(requester: User, target: User):
    notify('follow_request', target, actor=requester)


def _notify_new_follower(follower: User, following: User):
    notify('follow_new', following, actor=follower)


def _notify_request_accepted(accepter: User, requester: User):
    notify('follow_accepted', requester, actor=accepter)


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
    FollowRequest.objects.filter(Q(requester=user_a, target=user_b) | Q(requester=user_b, target=user_a)).delete()


def _accept_request(request: FollowRequest, *, notify_requester: bool = True) -> Follow:
    follow, _ = _create_follow(request.requester, request.target, notify=False)
    request.status = 'accepted'
    request.save(update_fields=['status', 'updated_at'])
    if notify_requester:
        _notify_request_accepted(request.target, request.requester)
    return follow


def _establish_mutual(requester: User, target: User, incoming_request: FollowRequest | None = None):
    """Accepte la demande entrante et crée le suivi retour sans nouvelle validation."""
    if incoming_request:
        _accept_request(incoming_request, notify_requester=True)
    elif FollowRequest.objects.filter(requester=target, target=requester, status='pending').exists():
        req = FollowRequest.objects.select_for_update().get(requester=target, target=requester, status='pending')
        _accept_request(req, notify_requester=True)

    _create_follow(requester, target, notify=False)
    _resolve_pending_request(requester, target, new_status='accepted')
    _resolve_pending_request(target, requester, new_status='accepted')


def make_mutual_friends(user_a: User, user_b: User) -> bool:
    """
    Amitié mutuelle consentie hors flux de demande (lien d'invitation : le parrain a partagé le lien,
    l'invité l'a utilisé). Crée les deux suivis sans notification « te suit » et solde les demandes.
    Retourne True si au moins un suivi a été créé.
    """
    _, created_ab = _create_follow(user_a, user_b, notify=False)
    _, created_ba = _create_follow(user_b, user_a, notify=False)
    _resolve_pending_request(user_a, user_b, new_status='accepted')
    _resolve_pending_request(user_b, user_a, new_status='accepted')
    return created_ab or created_ba


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
        FollowRequest.objects.select_for_update().filter(requester=target, target=requester, status='pending').first()
    )
    if incoming:
        _establish_mutual(requester, target, incoming)
        return {
            'action': 'mutual_established',
            'message': 'Vous êtes maintenant amis',
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
