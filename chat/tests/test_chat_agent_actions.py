"""Agent v2 « tout actionnable » (ag03) : import par lien, dater / déplacer / retirer un repas, inviter (LLM mocké)."""

import asyncio
from datetime import timedelta
from unittest import mock

import pytest

from chat.models import Conversation, Message, PendingAction
from chat.services import planning_tools
from chat.services.action_executor import execute_pending_action
from chat.services.async_tools import MUTATION_TOOL_NAMES, _start_import_from_url
from chat.services.tool_schemas import ImportJobStarted, RecipeImportResult
from recipes.models import BasketItem, MealInvitation, MealPlan, MealPlanRecipeBatch, RecipeImportRequest
from recipes.services import basket_service
from tables import services as table_services
from tables.models import TableMember

pytestmark = pytest.mark.django_db

NEW_TOOL_NAMES = {
    'import_recipe_from_url',
    'schedule_basket_item',
    'move_meal',
    'remove_meal',
    'invite_to_meal',
    'invite_to_table',
}


def _replay(api, user, undo):
    """Rejoue un UndoHint comme l'app (méthode + chemin + corps JSON)."""
    client = api.as_user(user)
    call = getattr(client, undo.method.lower())
    response = call(undo.path, undo.body or {}, format='json')
    assert response.status_code in (200, 201, 204), response.content
    return response


def _day(user, offset: int):
    return basket_service.user_today(user) + timedelta(days=offset)


@pytest.fixture
def friend(make_user, user, follow):
    lea = make_user('lea_cuisine', first_name='Léa')
    follow(user, lea)
    return lea


# ---------------------------------------------------------------------------
# import_recipe_from_url
# ---------------------------------------------------------------------------
def test_import_from_url_queues_the_pipeline_and_returns_a_pending_card(user, api, celery_delay):
    url = 'https://www.marmiton.org/recettes/recette_gratin-dauphinois_19114.aspx'
    card = planning_tools.start_recipe_import(user, url, conversation_id=42)

    req = RecipeImportRequest.objects.get(id=card.request_id)
    assert celery_delay.calls == [('recipes.tasks.process_recipe_import_from_url', (str(req.id),), {})]
    assert req.payload == {
        'url': url,
        'source_type': 'imported',
        'job_type': 'import',
        'chat_context': {'conversation_id': 42},
    }
    assert req.task_id
    assert isinstance(card, ImportJobStarted)  # l'app 1.0.9 reçoit toujours import_job_started
    assert (card.card_type, card.status, card.recipe_id, card.title) == ('recipe_import', 'pending', None, None)
    assert card.status_path == f'/api/recipes/formalize/status/{req.id}/'
    assert [a.kind for a in card.actions] == ['open_recipe', 'add_to_week']
    assert [a.label for a in card.actions] == ['Voir la recette', 'Ajouter à ma semaine']

    # Le front suit le job sur l'endpoint existant.
    polled = api.as_user(user).get(card.status_path)
    assert polled.status_code == 200 and polled.json()['status'] == 'pending'

    # Même lien recollé pendant l'import : même job, pas de second passage dans le pipeline.
    again = planning_tools.start_recipe_import(user, url + '?utm_source=insta', conversation_id=42)
    assert again.request_id == card.request_id and 'déjà en cours' in again.message
    assert len(celery_delay.calls) == 1


def test_import_from_url_returns_the_existing_recipe_without_a_job(user, make_recipe, celery_delay):
    recipe = make_recipe(user, 'Gratin dauphinois', import_source_url='https://www.example-blog.fr/gratin/')

    card = _start_import_from_url(user, 'https://www.example-blog.fr/gratin', conversation_id=1)

    assert isinstance(card, RecipeImportResult)
    assert celery_delay.calls == []
    assert (card.request_id, card.status, card.already_imported) == ('', 'success', True)
    assert (card.recipe_id, card.title) == (recipe.id, 'Gratin dauphinois')
    add = card.actions[1]
    assert (add.method, add.path, add.body) == ('POST', '/api/basket/', {'recipe_id': recipe.id})
    with pytest.raises(ValueError):
        planning_tools.start_recipe_import(user, '  ')


# ---------------------------------------------------------------------------
# schedule_basket_item / move_meal / remove_meal
# ---------------------------------------------------------------------------
def test_schedule_basket_item_dates_an_envie_and_undo_puts_it_back(user, api, make_user):
    item = basket_service.add_item(user, label='Risotto aux champignons')
    thursday = _day(user, 2)

    card = planning_tools.schedule_basket_item_for_user(
        user, basket_item_id=item.id, date=thursday.isoformat(), meal_time='soir'
    )

    item.refresh_from_db()
    assert card.card_type == 'meal_scheduled'
    assert (card.date, card.meal_time, card.meal_plan_id) == (thursday.isoformat(), 'dinner', item.meal_plan_id)
    assert card.previous_date is None
    assert (card.undo.method, card.undo.path) == ('DELETE', f'/api/basket/{item.id}/schedule/')
    assert 'Risotto aux champignons' in card.message

    _replay(api, user, card.undo)
    item.refresh_from_db()
    assert item.meal_plan_id is None

    # Ownership : l'envie d'un autre est introuvable ; jamais dans le passé.
    other_item = basket_service.add_item(make_user(), label='Pas à moi')
    with pytest.raises(ValueError, match='introuvable'):
        planning_tools.schedule_basket_item_for_user(user, basket_item_id=other_item.id, date=thursday.isoformat())
    with pytest.raises(ValueError, match='passée'):
        planning_tools.schedule_basket_item_for_user(user, basket_item_id=item.id, date='hier')


def test_rescheduling_a_dated_envie_undoes_to_the_previous_slot(user, api):
    item = basket_service.add_item(user, label='Tarte aux poireaux')
    first = planning_tools.schedule_basket_item_for_user(user, basket_item_id=item.id, date='demain', meal_time='midi')

    card = planning_tools.schedule_basket_item_for_user(
        user, basket_item_id=item.id, date=_day(user, 3).isoformat(), meal_time='dinner'
    )

    assert (card.previous_date, card.previous_meal_time) == (first.date, 'lunch')
    assert card.undo.method == 'POST' and card.undo.body == {'date': first.date, 'meal_time': 'lunch'}
    _replay(api, user, card.undo)
    item.refresh_from_db()
    assert (item.meal_plan.date.isoformat(), item.meal_plan.meal_time) == (first.date, 'lunch')


def test_move_meal_moves_the_whole_meal_and_its_envie_follows_the_week(user, api, make_recipe, friend):
    recipe = make_recipe(user, 'Lasagnes')
    item = basket_service.add_item(user, recipe_id=recipe.id)
    planning_tools.schedule_basket_item_for_user(user, basket_item_id=item.id, date='demain')
    item.refresh_from_db()
    meal = item.meal_plan
    MealInvitation.objects.create(inviter=user, invitee=friend, meal_plan=meal, status='accepted')
    target = _day(user, 8)  # semaine suivante

    card = planning_tools.move_meal_for_user(user, meal_plan_id=meal.id, date=target.isoformat(), meal_time='lunch')

    meal.refresh_from_db()
    item.refresh_from_db()
    assert card.card_type == 'meal_moved' and card.meal_plan_id == meal.id
    assert (meal.date, meal.meal_time) == (target, 'lunch')
    assert item.week_start == basket_service.monday_of(target)  # l'envie suit le repas
    assert MealInvitation.objects.filter(meal_plan=meal).count() == 1  # invitations gardées
    assert card.warnings and 'lea_cuisine' in card.warnings[0]
    assert card.undo.method == 'PATCH' and card.undo.path == f'/api/meal-plans/{meal.id}/update-composer-slot/'

    _replay(api, user, card.undo)
    meal.refresh_from_db()
    item.refresh_from_db()
    assert (meal.date.isoformat(), meal.meal_time) == (card.previous_date, 'dinner')
    assert item.week_start == basket_service.monday_of(meal.date)


def test_move_meal_refuses_someone_else_meal(user, make_user, make_meal_plan):
    meal = make_meal_plan(make_user(), day=_day(user, 1))
    with pytest.raises(ValueError, match='hôte'):
        planning_tools.move_meal_for_user(user, meal_plan_id=meal.id, date=_day(user, 2).isoformat())
    meal.refresh_from_db()
    assert meal.date == _day(user, 1)


def test_remove_meal_turns_its_dishes_back_into_envies_and_undo_restores(user, api, make_recipe):
    soup = make_recipe(user, 'Velouté')
    item = basket_service.add_item(user, recipe_id=make_recipe(user, 'Lasagnes').id)
    day = _day(user, 1)
    planning_tools.schedule_basket_item_for_user(user, basket_item_id=item.id, date=day.isoformat())
    item.refresh_from_db()
    meal = item.meal_plan
    # Plat ajouté au repas hors panier (planning v1).
    from recipes.services.meal_plan_service import add_recipes_to_meal_plan

    add_recipes_to_meal_plan(user, meal.id, [soup.id])

    card = planning_tools.remove_meal_for_user(user, meal_plan_id=meal.id)

    assert card.card_type == 'meal_removed' and card.meal_plan_id is None
    assert not MealPlan.objects.filter(id=meal.id).exists()
    envies = BasketItem.objects.filter(user=user).order_by('id')
    assert [(e.display_title, e.meal_plan_id) for e in envies] == [('Lasagnes', None), ('Velouté', None)]
    assert {d.basket_item_id for d in card.dishes} == {e.id for e in envies}
    assert card.undo is None and 'reste(nt) dans ton panier' in card.message

    for dish in card.dishes:
        assert dish.undo.body == {'date': day.isoformat(), 'meal_time': 'dinner'}
        _replay(api, user, dish.undo)
    envies = BasketItem.objects.filter(user=user)
    assert {e.meal_plan.date for e in envies} == {day}
    assert len({e.meal_plan_id for e in envies}) == 1  # de nouveau un seul repas


def test_remove_meal_for_one_dated_envie_and_refusals(user, api, make_user, make_meal_plan, friend):
    item = basket_service.add_item(user, label='Quiche')
    planning_tools.schedule_basket_item_for_user(user, basket_item_id=item.id, date='demain', meal_time='lunch')

    card = planning_tools.remove_meal_for_user(user, basket_item_id=item.id)
    item.refresh_from_db()
    assert item.meal_plan_id is None and card.undo.method == 'POST'
    _replay(api, user, card.undo)
    item.refresh_from_db()
    assert item.meal_plan.meal_time == 'lunch'

    with pytest.raises(ValueError, match='remove_from_basket'):
        planning_tools.remove_meal_for_user(user, basket_item_id=basket_service.add_item(user, label='X').id)
    with pytest.raises(ValueError, match='hôte'):
        planning_tools.remove_meal_for_user(user, meal_plan_id=make_meal_plan(make_user(), day=_day(user, 1)).id)
    invited = make_meal_plan(user, day=_day(user, 2), meal_time='dinner')
    MealInvitation.objects.create(inviter=user, invitee=friend, meal_plan=invited, status='pending')
    with pytest.raises(ValueError, match='invité'):
        planning_tools.remove_meal_for_user(user, meal_plan_id=invited.id)
    assert MealPlan.objects.filter(id=invited.id).exists()


def test_remove_an_empty_meal_and_recreate_it_with_undo(user, api, make_meal_plan):
    meal = make_meal_plan(user, day=_day(user, 1), meal_time='lunch', meal_type='cantine')

    card = planning_tools.remove_meal_for_user(user, meal_plan_id=meal.id)

    assert not MealPlan.objects.filter(id=meal.id).exists() and card.dishes == []
    assert (card.undo.method, card.undo.path) == ('POST', '/api/meal-plans/')
    _replay(api, user, card.undo)
    restored = MealPlan.objects.get(user=user, date=_day(user, 1))
    assert (restored.meal_time, restored.meal_type) == ('lunch', 'cantine')


# ---------------------------------------------------------------------------
# Invitations : ami·es seulement
# ---------------------------------------------------------------------------
def _pending_action(user, proposal):
    """Même enregistrement que le consumer quand le stream s'arrête sur invite_to_meal."""
    import uuid

    from chat.consumers import _save_mutation

    conv = Conversation.objects.create(user=user)
    msg, action = _save_mutation(conv, uuid.uuid4(), proposal, 'invite_to_meal')
    assert msg.message_type == Message.TYPE_MUTATION_PROPOSAL
    return action


def test_invite_to_meal_proposes_a_confirmation_card_for_friends_only(user, make_user, make_meal_plan, friend):
    stranger = make_user('marc')
    meal = make_meal_plan(user, day=_day(user, 2), meal_time='dinner')

    proposal = planning_tools.propose_invite_to_meal(
        user, date=_day(user, 2).isoformat(), meal_time='soir', invitee_names=['Léa', 'marc']
    )

    assert proposal.card_type == 'meal_invitation'  # carte de confirmation existante (app 1.0.9 comprise)
    assert proposal.details['meal_plan_ids'] == [meal.id]
    assert proposal.details['invitee_ids'] == [friend.id]
    assert any('marc' in w for w in proposal.warnings)
    assert MealInvitation.objects.count() == 0  # rien d'envoyé avant confirmation

    action = _pending_action(user, proposal)
    assert action.action_type == PendingAction.ACTION_MEAL_INVITATION
    execute_pending_action(user, action)
    assert list(MealInvitation.objects.values_list('invitee_id', 'meal_plan_id')) == [(friend.id, meal.id)]
    assert 'invite_to_meal' in MUTATION_TOOL_NAMES

    with pytest.raises(ValueError, match='ami'):
        planning_tools.propose_invite_to_meal(user, meal_plan_id=meal.id, invitee_ids=[stranger.id])
    with pytest.raises(ValueError, match='hôte'):
        planning_tools.propose_invite_to_meal(
            user, meal_plan_id=make_meal_plan(stranger, day=_day(user, 1)).id, invitee_names=['Léa']
        )
    undated = basket_service.add_item(user, label='Pizza')
    with pytest.raises(ValueError, match='schedule_basket_item'):
        planning_tools.propose_invite_to_meal(user, basket_item_id=undated.id, invitee_names=['Léa'])


def test_invite_to_table_invites_friends_and_owner_can_undo(user, api, make_user, friend):
    stranger = make_user('marc')
    table, _ = table_services.create_table(user, name='Famille', emoji='🏠')

    card = planning_tools.invite_to_table_for_user(user, table_name='famille', invitee_names=['lea'])

    assert card.card_type == 'table_invitation' and card.table_id == table.id
    assert [(i.user_id, i.status) for i in card.invitees] == [(friend.id, 'invited')]
    member = TableMember.objects.get(table=table, user=friend)
    assert member.status == TableMember.STATUS_INVITED
    assert card.undo.path == f'/api/tables/{table.id}/members/{friend.id}/'
    assert card.actions[0].kind == 'open_table'

    again = planning_tools.invite_to_table_for_user(user, invitee_ids=[friend.id])  # sa seule tablée
    assert again.invitees[0].status == 'already_invited' and again.undo is None

    _replay(api, user, card.undo)
    assert not TableMember.objects.filter(table=table, user=friend).exists()

    with pytest.raises(ValueError, match='ami'):
        planning_tools.invite_to_table_for_user(user, table_id=table.id, invitee_ids=[stranger.id])
    with pytest.raises(ValueError, match='introuvable'):
        planning_tools.invite_to_table_for_user(user, table_id=999999, invitee_names=['Léa'])


def _save_card(user, proposal, tool_name):
    import uuid

    from chat.consumers import _save_mutation

    conv = Conversation.objects.create(user=user)
    msg, action = _save_mutation(conv, uuid.uuid4(), proposal, tool_name)
    assert msg.message_type == Message.TYPE_MUTATION_PROPOSAL
    return action


def test_invite_to_table_tool_goes_through_a_confirmation_card(user, make_user, friend):
    """Injection de prompt (contenu importé) : l'outil ne fait que proposer, rien n'est envoyé avant confirmation."""
    stranger = make_user('marc')
    table, _ = table_services.create_table(user, name='Famille', emoji='🏠')

    proposal = planning_tools.propose_invite_to_table(user, table_name='famille', invitee_names=['Léa', 'marc'])

    assert 'invite_to_table' in MUTATION_TOOL_NAMES
    assert proposal.card_type == 'table_invitation_proposal'  # ≠ carte résultat `table_invitation` de l'app
    assert proposal.details == {
        'table_id': table.id,
        'table_name': 'Famille',
        'invitee_ids': [friend.id],
        'invitee_usernames': [friend.username],
    }
    assert any('marc' in w for w in proposal.warnings)
    assert not TableMember.objects.filter(table=table, user=friend).exists()  # rien d'envoyé

    action = _save_card(user, proposal, 'invite_to_table')
    assert action.action_type == PendingAction.ACTION_TABLE_INVITATION
    result = execute_pending_action(user, action)
    assert result['invitees'][0]['status'] == 'invited'
    assert TableMember.objects.get(table=table, user=friend).status == TableMember.STATUS_INVITED

    with pytest.raises(ValueError, match='Déjà'):
        planning_tools.propose_invite_to_table(user, table_id=table.id, invitee_ids=[friend.id])
    with pytest.raises(ValueError, match='ami'):
        planning_tools.propose_invite_to_table(user, table_id=table.id, invitee_ids=[stranger.id])
    # Payload confirmé revérifié : une tablée dont on n'est pas membre est refusée à l'exécution.
    other_table, _ = table_services.create_table(stranger, name='Autre')
    forged = _save_card(
        user,
        proposal.model_copy(update={'details': {'table_id': other_table.id, 'invitee_ids': [friend.id]}}),
        'invite_to_table',
    )
    with pytest.raises(ValueError, match='introuvable'):
        execute_pending_action(user, forged)


def test_add_shopping_list_item_needs_confirmation_only_on_a_shared_list(user, make_user, make_shopping_list):
    from chat.services.tool_schemas import AddShoppingListItemResult, MutationProposal
    from recipes.models import ShoppingListItem

    mine = make_shopping_list(user, name='Perso')
    shared = make_shopping_list(user, name='Coloc', collaborators=[make_user('pauline')])
    assert 'add_shopping_list_item' in MUTATION_TOOL_NAMES

    # Sa propre liste : ajout immédiat, pas de carte.
    own = planning_tools.add_or_propose_shopping_list_item(user, mine.id, 'Beurre', quantity=250, unit='g')
    assert isinstance(own, AddShoppingListItemResult)
    assert ShoppingListItem.objects.filter(shopping_list=mine, ingredient__name__iexact='beurre').exists()

    # Liste partagée : carte de confirmation, rien d'écrit.
    proposal = planning_tools.add_or_propose_shopping_list_item(user, shared.id, '  Vin   rouge ', quantity=6)
    assert isinstance(proposal, MutationProposal)
    assert proposal.card_type == 'shopping_list_item_proposal'
    assert proposal.details['shared_with'] == ['pauline']
    assert proposal.details['ingredient_name'] == 'Vin rouge'
    assert not ShoppingListItem.objects.filter(shopping_list=shared).exists()

    action = _save_card(user, proposal, 'add_shopping_list_item')
    assert action.action_type == PendingAction.ACTION_SHOPPING_LIST_ITEM
    result = execute_pending_action(user, action)
    assert result['shopping_list_id'] == shared.id and result['quantity'] == 6
    assert ShoppingListItem.objects.filter(shopping_list=shared, ingredient__name__iexact='vin rouge').exists()

    # Liste d'un autre : refusée à la proposition comme à l'exécution d'un payload forgé.
    foreign = make_shopping_list(make_user('marc'), name='Marc')
    with pytest.raises(ValueError, match='accès refusé'):
        planning_tools.add_or_propose_shopping_list_item(user, foreign.id, 'Sel')
    forged = _save_card(
        user, proposal.model_copy(update={'details': {**proposal.details, 'shopping_list_id': foreign.id}}), 'x'
    )
    forged.action_type = PendingAction.ACTION_SHOPPING_LIST_ITEM
    forged.save(update_fields=['action_type'])
    with pytest.raises(ValueError, match='accès refusé'):
        execute_pending_action(user, forged)
    assert not ShoppingListItem.objects.filter(shopping_list=foreign).exists()


def test_session_context_lists_tables_with_ids(user):
    from chat.services.session_context import build_profile_context

    table, _ = table_services.create_table(user, name='Copains du jeudi')
    assert f'Tablées : Copains du jeudi [#{table.id}]' in build_profile_context(user)


# ---------------------------------------------------------------------------
# Registre, prompt, aller-retour tool calling (client mocké)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_api_key():
    with mock.patch('chat.services.agent_factory._api_key', return_value='test-key'):
        yield


def test_registry_exposes_old_and_new_tools_and_prompt_lists_them(user, fake_api_key):
    from chat.services.agent_factory import create_planning_agent
    from chat.tests.test_chat_agent_v2 import V1_TOOL_NAMES

    agent = create_planning_agent(user, session_context='Contexte de test')
    names = {t.name for t in agent.tools}
    assert V1_TOOL_NAMES <= names
    assert {'plan_week', 'log_cooked', 'remove_from_basket', 'undo_log_cooked', 'battle_plan'} <= names
    assert NEW_TOOL_NAMES <= names
    for name in NEW_TOOL_NAMES:
        assert name in agent.instructions
    assert 'colle un lien de recette, importe-le directement' in agent.instructions
    assert 'sans demander de confirmation' in agent.instructions
    lowered = agent.instructions.lower()
    assert 'strava' not in lowered and 'bande' not in lowered


def test_import_tool_round_trip_through_openai_compatible_client(user, fake_api_key):
    from agents import RunConfig, Runner

    from chat.services.agent_context import AgentContext
    from chat.services.agent_factory import create_planning_agent
    from chat.tests.test_chat_agent_v2 import _completion

    agent = create_planning_agent(user, session_context='Contexte de test')
    calls = []
    url = 'https://www.instagram.com/p/abc123/'
    replies = [
        _completion(
            {
                'role': 'assistant',
                'content': None,
                'tool_calls': [
                    {
                        'id': 'call_1',
                        'type': 'function',
                        'function': {'name': 'import_recipe_from_url', 'arguments': f'{{"url": "{url}"}}'},
                    }
                ],
            },
            'tool_calls',
        ),
        _completion({'role': 'assistant', 'content': 'La recette arrive.'}, 'stop'),
    ]

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return replies[len(calls) - 1]

    agent.model._client.chat.completions.create = fake_create
    started = RecipeImportResult(request_id='r-1', url=url, message='Import lancé.')
    with mock.patch.object(planning_tools, 'start_recipe_import', return_value=started) as service:
        result = asyncio.run(
            Runner.run(
                agent,
                f'Regarde ça {url}',
                context=AgentContext(user=user, conversation_id=7),
                run_config=RunConfig(tracing_disabled=True),
            )
        )

    assert result.final_output == 'La recette arrive.'
    assert service.call_args.args[1:] == (url, 7)
    tool_msg = next(m for m in calls[1]['messages'] if m.get('role') == 'tool')
    assert "card_type='recipe_import'" in tool_msg['content']


def test_meal_plan_recipe_batches_untouched_by_schedule_of_free_dish(user):
    """Garde-fou : dater un plat libre ne crée pas de batch (aucune recette)."""
    item = basket_service.add_item(user, label='Raclette')
    card = planning_tools.schedule_basket_item_for_user(user, basket_item_id=item.id, date='demain')
    assert not MealPlanRecipeBatch.objects.filter(meal_plan_id=card.meal_plan_id).exists()
