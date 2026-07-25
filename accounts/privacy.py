"""
Règles de visibilité entre utilisateurs (amis, profil, feed).

- **Profil / posts** : visibles seulement si le viewer suit le propriétaire (Follow approuvé).
- **Réseau complice** : au moins une relation Follow dans un sens ou l'autre
  (invitations repas, listes de courses, etc.).
- **Amis mutuels** : les deux se suivent (menu allergies, etc.).
"""
from django.db.models import Q

from .blocks import are_blocked_either_way
from .models import Follow


def are_complices_in_network(viewer, target) -> bool:
    """Viewer et target ont une relation complice (suivi dans au moins un sens)."""
    if not viewer or not target:
        return False
    if getattr(viewer, 'id', None) == getattr(target, 'id', None):
        return True
    return Follow.objects.filter(
        Q(follower=viewer, following=target) | Q(follower=target, following=viewer)
    ).exists()


def are_mutual_complices(viewer, target) -> bool:
    """Les deux utilisateurs se suivent mutuellement (profil « ami »)."""
    if not viewer or not target:
        return False
    if getattr(viewer, 'id', None) == getattr(target, 'id', None):
        return True
    a = Follow.objects.filter(follower=viewer, following=target).exists()
    b = Follow.objects.filter(follower=target, following=viewer).exists()
    return a and b


def can_view_profile_content(viewer, profile_user) -> bool:
    """
    Carnet / posts d'un profil : visibles seulement si le viewer suit le propriétaire
    (relation Follow approuvée en base après acceptation de la demande).
    """
    if not viewer or not profile_user:
        return False
    if getattr(viewer, 'id', None) == getattr(profile_user, 'id', None):
        return True
    if not getattr(viewer, 'is_authenticated', False):
        return False
    if are_blocked_either_way(viewer, profile_user):
        return False
    return Follow.objects.filter(follower=viewer, following=profile_user).exists()


def can_view_dietary_preferences(viewer, profile_user) -> bool:
    """Allergies / goûts : visibles seulement pour soi ou complices mutuels."""
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return False
    if viewer.id == profile_user.id:
        return True
    return are_mutual_complices(viewer, profile_user)
