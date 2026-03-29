"""
Règles de visibilité entre utilisateurs (complices, profil).

- **Réseau complice** : au moins une relation Follow dans un sens ou l'autre
  (aligné sur les invitations repas / liste complices).
- **Complices mutuels** : les deux se suivent (ami au sens « isComplice » côté app).
"""
from django.db.models import Q

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


def can_view_dietary_preferences(viewer, profile_user) -> bool:
    """Allergies / goûts : visibles seulement pour soi ou complices mutuels."""
    if not viewer or not getattr(viewer, 'is_authenticated', False):
        return False
    if viewer.id == profile_user.id:
        return True
    return are_mutual_complices(viewer, profile_user)
