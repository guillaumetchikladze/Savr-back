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
    elif action.action_type == PendingAction.ACTION_TABLE_INVITATION:
        result = _execute_table_invitation(user, payload)
    elif action.action_type == PendingAction.ACTION_SHOPPING_LIST_ITEM:
        result = _execute_shopping_list_item(user, payload)
    else:
        raise ValueError(f"Type d'action inconnu: {action.action_type}")

    action.status = PendingAction.STATUS_CONFIRMED
    action.save(update_fields=['status'])
    return result


def _execute_table_invitation(user: AbstractBaseUser, payload: dict) -> dict:
    """Carte `table_invitation_proposal` confirmée : tout est revérifié (membre de la tablée, ami·es)."""
    from chat.services.planning_tools import invite_to_table_for_user

    return invite_to_table_for_user(
        user, table_id=payload.get('table_id'), invitee_ids=payload.get('invitee_ids') or []
    ).model_dump(mode='json')


def _execute_shopping_list_item(user: AbstractBaseUser, payload: dict) -> dict:
    """Carte `shopping_list_item_proposal` confirmée : accès à la liste revérifié."""
    from recipes.services.shopping_list_service import add_item_to_shopping_list

    return add_item_to_shopping_list(
        user,
        payload.get('shopping_list_id'),
        payload.get('ingredient_name') or '',
        quantity=payload.get('quantity') or 1,
        unit=payload.get('unit') or 'piece',
    ).model_dump(mode='json')
