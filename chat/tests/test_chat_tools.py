"""Outils de l'agent chat au niveau service (LLM mocké) : proposition → confirmation → exécution."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from chat.models import Conversation, Message, PendingAction
from chat.services.action_executor import execute_pending_action
from chat.services.async_tools import _start_generate_from_idea
from recipes.models import RecipeImportRequest
from recipes.services.pydantic_models import RecipeFormalized, RecipeIngredientFormalized, StepFormalized
from recipes.services.recipe_revision_service import propose_recipe_revision_data

pytestmark = pytest.mark.django_db


def _pending_action(user, action_type, payload, *, expires_in=timedelta(minutes=10)):
    conv = Conversation.objects.create(user=user)
    msg = Message.objects.create(conversation=conv, role=Message.ROLE_ASSISTANT, content='proposition')
    return PendingAction.objects.create(
        conversation=conv,
        message=msg,
        action_type=action_type,
        payload=payload,
        expires_at=timezone.now() + expires_in,
    )


def _revised(title, description='Plus léger'):
    return RecipeFormalized(
        title=title,
        description=description,
        steps_summary='Version allégée.',
        prep_time=10,
        cook_time=20,
        servings=2,
        recipe_ingredients=[RecipeIngredientFormalized(ingredient_name='courgette', quantity=Decimal('300'), unit='g')],
        steps=[StepFormalized(order=1, instruction='Cuire.')],
    )


def test_recipe_revision_is_proposed_then_applied_on_confirmation(user, make_recipe):
    recipe = make_recipe(user, 'Gratin lourd', ingredients=['crème'])

    with mock.patch(
        'recipes.services.recipe_revision_service.revise_recipe',
        new=mock.AsyncMock(return_value=_revised('Gratin léger')),
    ) as llm:
        proposal = propose_recipe_revision_data(user, recipe.id, 'Allège la recette')

    llm.assert_awaited_once()
    assert proposal.card_type == 'recipe_revision'
    recipe.refresh_from_db()
    assert recipe.title == 'Gratin lourd'  # rien n'est appliqué avant confirmation

    action = _pending_action(user, PendingAction.ACTION_RECIPE_REVISION, proposal.details)
    execute_pending_action(user, action)

    recipe.refresh_from_db()
    assert recipe.title == 'Gratin léger'
    assert list(recipe.recipe_ingredients.values_list('ingredient__name', flat=True)) == ['courgette']
    action.refresh_from_db()
    assert action.status == PendingAction.STATUS_CONFIRMED
    with pytest.raises(ValueError, match='déjà traitée'):
        execute_pending_action(user, action)


def test_recipe_revision_without_description_keeps_the_existing_one(user, make_recipe):
    recipe = make_recipe(user, 'Gratin lourd', ingredients=['crème'], description='Recette de mamie')
    with mock.patch(
        'recipes.services.recipe_revision_service.revise_recipe',
        new=mock.AsyncMock(return_value=_revised('Gratin léger', description=None)),
    ):
        proposal = propose_recipe_revision_data(user, recipe.id, 'Allège la recette')

    execute_pending_action(user, _pending_action(user, PendingAction.ACTION_RECIPE_REVISION, proposal.details))

    recipe.refresh_from_db()
    assert (recipe.title, recipe.description) == ('Gratin léger', 'Recette de mamie')


def test_recipe_revision_refused_for_someone_else_recipe(user, make_user, make_recipe):
    recipe = make_recipe(make_user())
    with mock.patch('recipes.services.recipe_revision_service.revise_recipe') as llm, pytest.raises(ValueError):
        propose_recipe_revision_data(user, recipe.id, 'Allège la recette')
    llm.assert_not_called()


def test_expired_action_is_not_executed(user, make_recipe):
    action = _pending_action(
        user,
        PendingAction.ACTION_RECIPE_REVISION,
        {'recipe_id': make_recipe(user).id, 'after_formalized': {'title': 'x'}},
        expires_in=timedelta(minutes=-1),
    )
    with pytest.raises(ValueError, match='expirée'):
        execute_pending_action(user, action)


def test_generate_from_idea_tool_queues_job_linked_to_conversation(user, celery_delay):
    started = _start_generate_from_idea(user, 'Un curry de lentilles corail', 4, conversation_id=42)

    req = RecipeImportRequest.objects.get(id=started.request_id)
    assert req.payload['chat_context'] == {'conversation_id': 42}
    assert req.task_id == 'fake-task-1'
    assert celery_delay.calls == [('recipes.tasks.process_recipe_generate_from_idea', (str(req.id),), {})]

    with pytest.raises(ValueError):
        _start_generate_from_idea(user, 'abc')
