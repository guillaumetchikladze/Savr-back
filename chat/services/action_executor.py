"""Exécution backend-only des actions confirmées (hors LLM)."""

from django.contrib.auth.models import AbstractBaseUser

from chat.models import PendingAction
from recipes.services.invitation_service import execute_meal_invitation
from recipes.services.meal_plan_service import execute_meal_deletion
from recipes.services.recipe_revision_service import execute_recipe_revision


def execute_pending_action(user: AbstractBaseUser, action: PendingAction) -> dict:
    if action.status != PendingAction.STATUS_PENDING:
        raise ValueError(f'Action déjà traitée ({action.status}).')
    if action.mark_expired_if_needed():
        raise ValueError('Action expirée.')

    payload = action.payload
    if action.action_type == PendingAction.ACTION_MEAL_DELETION:
        result = execute_meal_deletion(user, payload)
    elif action.action_type == PendingAction.ACTION_MEAL_INVITATION:
        result = execute_meal_invitation(user, payload)
    elif action.action_type == PendingAction.ACTION_RECIPE_REVISION:
        result = execute_recipe_revision(user, payload)
    else:
        raise ValueError(f'Type d\'action inconnu: {action.action_type}')

    action.status = PendingAction.STATUS_CONFIRMED
    action.save(update_fields=['status'])
    return result
