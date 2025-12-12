from django.db.models import Q


def get_accessible_meal_plan_filter(user):
    """
    Retourne un Q object pour filtrer les MealPlan auxquels un utilisateur a accès :
    - Les MealPlan dont il est le propriétaire
    - Les MealPlan auxquels il est invité avec une invitation acceptée
    """
    return Q(
        Q(user=user) |  # Propriétaire
        Q(invitations__invitee=user, invitations__status='accepted')  # Invité accepté
    )

