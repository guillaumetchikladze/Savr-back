"""Helpers de filtrage pour les blocages utilisateurs (Guideline Apple 1.2)."""
from __future__ import annotations

from django.db.models import Q

from .models import UserBlock


def are_blocked_either_way(user_a, user_b) -> bool:
    """True si un blocage existe dans un sens ou l'autre entre A et B."""
    if not user_a or not user_b:
        return False
    a_id = getattr(user_a, 'id', None)
    b_id = getattr(user_b, 'id', None)
    if not a_id or not b_id or a_id == b_id:
        return False
    return UserBlock.objects.filter(
        Q(blocker_id=a_id, blocked_id=b_id) | Q(blocker_id=b_id, blocked_id=a_id)
    ).exists()


def blocked_user_ids_for(user) -> set[int]:
    """IDs des utilisateurs bloqués par `user` ou qui le bloquent."""
    if not user or not getattr(user, 'id', None):
        return set()
    uid = user.id
    blocked_by_me = UserBlock.objects.filter(blocker_id=uid).values_list('blocked_id', flat=True)
    blocking_me = UserBlock.objects.filter(blocked_id=uid).values_list('blocker_id', flat=True)
    return set(blocked_by_me) | set(blocking_me)


def exclude_blocked_from_user_qs(queryset, viewer, id_field: str = 'id'):
    """Exclut du queryset User (ou annoté) les comptes en relation de blocage avec viewer."""
    ids = blocked_user_ids_for(viewer)
    if not ids:
        return queryset
    return queryset.exclude(**{f'{id_field}__in': ids})
