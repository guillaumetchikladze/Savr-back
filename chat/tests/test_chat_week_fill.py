"""« Complète ma semaine » (wk02) : contexte calculé côté serveur, propositions sans écriture, actions rejouées au tap."""

import asyncio
from datetime import timedelta
from unittest import mock

import pytest

from accounts.models import HouseholdMember
from chat.services import week_fill
from chat.services.tool_schemas import WeekMealProposalsResult
from recipes.models import BasketItem, Collection, CollectionRecipe, MealInvitation, MealPlan
from recipes.services import basket_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake_api_key():
    with mock.patch('chat.services.agent_factory._api_key', return_value='test-key'):
        yield


def _weeks(user):
    today = basket_service.user_today(user)
    this_monday = basket_service.monday_of(today)
    return today, this_monday, this_monday + timedelta(days=7)


def _cooked(make_meal_plan, user, day, recipe):
    return make_meal_plan(user, day=day, meal_time='dinner', recipes=[recipe], confirmed=True)


# ---------------------------------------------------------------------------
# get_week_context
# ---------------------------------------------------------------------------
def test_week_context_lists_free_evenings_basket_last_week_history_book_and_constraints(
    make_user, user, make_recipe, make_meal_plan
):
    user.allergies = ['arachide']
    user.save()
    pauline = make_user('pauline', first_name='Pauline', allergies=['fruits à coque'])
    HouseholdMember.objects.create(owner=user, user=pauline, status=HouseholdMember.STATUS_ACCEPTED)
    HouseholdMember.objects.create(owner=user, name='Jules', is_child=True, status=HouseholdMember.STATUS_ACCEPTED)
    bob = make_user('bob')
    today, this_monday, next_monday = _weeks(user)

    risotto = make_recipe(user, 'Risotto aux champignons', cuisine='italienne')
    gratin = make_recipe(user, 'Gratin dauphinois', prep_time=20, cook_time=60)
    satay = make_recipe(user, 'Brochettes satay', ingredients=['poulet', 'arachide'])
    lasagnes = make_recipe(user, 'Lasagnes')
    pho = make_recipe(bob, 'Phở au bœuf')
    secret = make_recipe(bob, 'Recette privée de Bob', is_public=False)
    collection = Collection.objects.create(name='À tester', owner=user)
    for recipe in (pho, secret):
        CollectionRecipe.objects.create(collection=collection, recipe=recipe, added_by=user)

    # Historique : le risotto 3 fois (dont la semaine dernière = semaine en cours vue depuis la suivante).
    for day in (this_monday, today - timedelta(days=20), today - timedelta(days=40)):
        _cooked(make_meal_plan, user, day, risotto)
    # Semaine visée (la suivante) : mardi soir prévu, jeudi soir invité chez Bob, une envie sans date.
    make_meal_plan(user, day=next_monday + timedelta(days=1), meal_time='dinner', recipes=[lasagnes])
    bob_meal = make_meal_plan(bob, day=next_monday + timedelta(days=3), meal_time='dinner')
    MealInvitation.objects.create(inviter=bob, invitee=user, meal_plan=bob_meal, status='accepted')
    soup = basket_service.add_item(user, label='Soupe de potiron', week=next_monday.isoformat())
    tart = basket_service.add_item(user, label='Tarte fine', week=this_monday.isoformat())

    ctx = week_fill.build_week_context(user, next_monday.isoformat())

    assert (ctx.week_start, ctx.is_past_week, ctx.lunches_included) == (next_monday.isoformat(), False, False)
    free = [(s.date, s.meal_time) for s in ctx.empty_slots]
    expected = [((next_monday + timedelta(days=i)).isoformat(), 'dinner') for i in (0, 2, 4, 5, 6)]
    assert free == expected
    assert ctx.empty_slots[0].label == week_fill.slot_label(next_monday, 'dinner')
    planned = {(p.date, p.title) for p in ctx.planned}
    assert ((next_monday + timedelta(days=1)).isoformat(), 'Lasagnes') in planned
    assert ((next_monday + timedelta(days=3)).isoformat(), 'Chez bob') in planned
    assert [b.basket_item_id for b in ctx.basket_unscheduled] == [soup.id]
    assert [b.basket_item_id for b in ctx.last_week_not_done] == [tart.id]
    assert [(e.title, e.cooked) for e in ctx.last_week] == [('Risotto aux champignons', True)]

    fav = ctx.favourites[0]
    assert (fav.recipe_id, fav.times, fav.cuisine) == (risotto.id, 3, 'italienne')
    book_ids = [b.recipe_id for b in ctx.book]
    assert gratin.id in book_ids and pho.id in book_ids
    assert risotto.id not in book_ids  # fait il y a moins de 3 semaines
    assert satay.id not in book_ids  # allergie
    assert lasagnes.id not in book_ids  # déjà prévue
    assert secret.id not in book_ids  # recette privée d'un autre, même dans sa collection
    assert next(b for b in ctx.book if b.recipe_id == gratin.id).total_time_min == 80

    assert ctx.people == 3
    joined = ' '.join(ctx.constraints)
    assert 'arachide' in joined and 'Pauline' in joined and 'fruits à coque' in joined
    assert ctx.recent_cuisines == ['italienne ×2']
    assert ctx.cooked_per_week == 0.4
    assert ctx.target_count == 3  # rythme (≥ 3) borné par 5 soirs libres - 1 envie à caler
    assert ctx.message == '5 soirs libres, 1 envie à caler.'
    # Lecture seule.
    assert BasketItem.objects.filter(user=user).count() == 2


def test_week_context_today_after_lunch_and_lunch_habit(user, make_recipe, make_meal_plan):
    """Horloge des tests figée à 15 h : le midi du jour est passé, le soir encore libre."""
    today, this_monday, _ = _weeks(user)
    quiche = make_recipe(user, 'Quiche')
    for i in range(1, 4):
        make_meal_plan(user, day=today - timedelta(days=i), meal_time='lunch', recipes=[quiche])

    ctx = week_fill.build_week_context(user)

    assert ctx.lunches_included is True
    free = {(s.date, s.meal_time) for s in ctx.empty_slots}
    assert (today.isoformat(), 'dinner') in free and (today.isoformat(), 'lunch') not in free
    assert all(s.date >= today.isoformat() for s in ctx.empty_slots)
    assert ctx.week_start == this_monday.isoformat()

    past = week_fill.build_week_context(user, (this_monday - timedelta(days=7)).isoformat())
    assert past.is_past_week and past.empty_slots == [] and past.target_count == 0


# ---------------------------------------------------------------------------
# propose_week_meals
# ---------------------------------------------------------------------------
def test_propose_week_meals_builds_actions_without_writing(make_user, user, make_recipe):
    user.allergies = ['arachide']
    user.food_dislikes = ['coriandre']
    user.save()
    bob = make_user('bob')
    _, this_monday, next_monday = _weeks(user)
    gratin = make_recipe(user, 'Gratin dauphinois', prep_time=20, cook_time=60)
    satay = make_recipe(user, 'Brochettes satay', ingredients=['poulet', 'arachide'])
    secret = make_recipe(bob, 'Recette privée', is_public=False)
    soup = basket_service.add_item(user, label='Soupe de potiron', week=next_monday.isoformat())
    monday, wednesday = next_monday, next_monday + timedelta(days=2)
    before = (BasketItem.objects.count(), MealPlan.objects.count())

    result = week_fill.propose_week_meals_for_user(
        user,
        [
            {
                'basket_item_id': soup.id,
                'date': wednesday.isoformat(),
                'meal_time': 'dinner',
                'reason': 'déjà au panier',
            },
            {'recipe_id': gratin.id, 'date': monday.isoformat(), 'meal_time': 'soir', 'reason': 'ton carnet'},
            {'recipe_id': gratin.id, 'date': monday.isoformat()},  # doublon
            {'label': 'Soupe miso', 'date': this_monday.isoformat()},  # créneau hors semaine → sans date
            {'label': 'Bo bun', 'date': monday.isoformat()},  # même créneau que le gratin → sans date
            {'recipe_id': satay.id, 'date': monday.isoformat()},
            {'label': 'Curry à la coriandre'},
            {'recipe_id': secret.id},
            {'recipe_id': 999999},
        ],
        week=next_monday.isoformat(),
    )

    assert isinstance(result, WeekMealProposalsResult) and result.card_type == 'week_meal_proposals'
    assert (BasketItem.objects.count(), MealPlan.objects.count()) == before  # rien d'écrit
    by_title = {p.title: p for p in result.proposals}
    assert list(by_title) == ['Soupe de potiron', 'Gratin dauphinois', 'Soupe miso', 'Bo bun']

    envie = by_title['Soupe de potiron']
    assert (envie.source, envie.basket_item_id, envie.slot_label) == (
        'panier',
        soup.id,
        week_fill.slot_label(wednesday, 'dinner'),
    )
    assert [(a.kind, a.path, a.body) for a in envie.actions] == [
        ('schedule', f'/api/basket/{soup.id}/schedule/', {'date': wednesday.isoformat(), 'meal_time': 'dinner'})
    ]

    plat = by_title['Gratin dauphinois']
    assert (plat.source, plat.total_time_min, plat.meal_time, plat.reason) == ('carnet', 80, 'dinner', 'ton carnet')
    kinds = {a.kind: a for a in plat.actions}
    assert kinds['schedule'].label == week_fill.slot_button_label(monday, 'dinner')
    assert kinds['schedule'].body == {'recipe_id': gratin.id, 'date': monday.isoformat(), 'meal_time': 'dinner'}
    assert kinds['add_to_week'].body == {'recipe_id': gratin.id, 'week': next_monday.isoformat()}
    assert kinds['open_recipe'].recipe_id == gratin.id

    for title in ('Soupe miso', 'Bo bun'):
        free = by_title[title]
        assert free.date is None and free.is_free_text and free.source == 'nouveau'
        assert [a.kind for a in free.actions] == ['add_to_week']

    skipped = {(s.label, s.recipe_id): s.reason for s in result.skipped}
    assert skipped[('Brochettes satay', satay.id)] == 'allergy'
    assert skipped[('Curry à la coriandre', None)] == 'dislike'
    assert skipped[('', secret.id)] == 'recipe_not_found'
    assert skipped[('', 999999)] == 'recipe_not_found'
    assert result.message.startswith('4 propositions affichées : rien n’est encore ajouté')
    with pytest.raises(ValueError):
        week_fill.propose_week_meals_for_user(user, [])


def test_card_actions_replayed_by_the_app_create_and_schedule(api, user, make_recipe):
    """Les corps des boutons de la carte sont acceptés tels quels par l'API (et annulables)."""
    _, _, next_monday = _weeks(user)
    gratin = make_recipe(user, 'Gratin dauphinois')
    soup = basket_service.add_item(user, label='Soupe de potiron', week=next_monday.isoformat())
    friday = next_monday + timedelta(days=4)
    card = week_fill.propose_week_meals_for_user(
        user,
        [
            {'recipe_id': gratin.id, 'date': friday.isoformat(), 'meal_time': 'dinner'},
            {'basket_item_id': soup.id, 'date': next_monday.isoformat()},
        ],
        week=next_monday.isoformat(),
    )
    client = api.as_user(user)

    schedule = next(a for a in card.proposals[0].actions if a.kind == 'schedule')
    res = client.post(schedule.path, schedule.body, format='json')
    assert res.status_code == 201, res.content
    assert res.data['status'] == 'planned' and res.data['meal_plan']['date'] == friday.isoformat()
    assert res.data['week_start'] == next_monday.isoformat()
    item_id = res.data['id']
    assert client.delete(f'/api/basket/{item_id}/').status_code == 204  # « Annuler » côté app
    assert not BasketItem.objects.filter(id=item_id).exists()

    envie = card.proposals[1].actions[0]
    res = client.post(envie.path, envie.body, format='json')
    assert res.status_code == 200 and res.data['status'] == 'planned'
    assert res.data['meal_plan']['meal_time'] == 'dinner'

    # Créneau invalide : 400, et l'envie n'est pas créée à moitié (ajout + datation : tout ou rien).
    count = BasketItem.objects.count()
    bad = client.post(
        '/api/basket/', {'label': 'Raclette', 'date': friday.isoformat(), 'meal_time': 'brunch'}, format='json'
    )
    assert bad.status_code == 400 and BasketItem.objects.count() == count
    assert client.post('/api/basket/', {'label': 'Raclette', 'date': 'pas-une-date'}, format='json').status_code == 400
    # Sans date : comportement historique (envie non datée).
    plain = client.post('/api/basket/', {'label': 'Raclette'}, format='json')
    assert plain.status_code == 201 and plain.data['status'] == 'todo'


# ---------------------------------------------------------------------------
# Registre et format tool calling (client OpenAI-compatible mocké)
# ---------------------------------------------------------------------------
def test_tools_registered_with_a_flat_schema_and_prompt(user, fake_api_key):
    from chat.services.agent_factory import create_planning_agent

    agent = create_planning_agent(user, session_context='Contexte de test')
    tools = {t.name: t for t in agent.tools}
    assert {'get_week_context', 'propose_week_meals'} <= set(tools)
    assert '$ref' not in str(tools['propose_week_meals'].params_json_schema)
    assert '$defs' not in str(tools['propose_week_meals'].params_json_schema)
    assert 'Complète ma semaine' in agent.instructions
    assert "propose_week_meals n'écrit rien" in agent.instructions


def test_propose_week_meals_round_trip_through_openai_compatible_client(user, fake_api_key):
    from agents import RunConfig, Runner

    from chat.services.agent_context import AgentContext
    from chat.services.agent_factory import create_planning_agent
    from chat.tests.test_chat_agent_v2 import _completion

    agent = create_planning_agent(user, session_context='Contexte de test')
    calls = []
    args = '{"meals": [{"recipe_id": 12, "date": "2026-11-16", "meal_time": "dinner", "reason": "ton classique"}]}'
    replies = [
        _completion(
            {
                'role': 'assistant',
                'content': None,
                'tool_calls': [
                    {'id': 'call_1', 'type': 'function', 'function': {'name': 'propose_week_meals', 'arguments': args}}
                ],
            },
            'tool_calls',
        ),
        _completion({'role': 'assistant', 'content': 'Voilà pour tes soirs libres.'}, 'stop'),
    ]

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return replies[len(calls) - 1]

    agent.model._client.chat.completions.create = fake_create
    card = WeekMealProposalsResult(week_start='2026-11-16', week_end='2026-11-22', message='1 proposition.')
    with mock.patch.object(week_fill, 'propose_week_meals_for_user', return_value=card) as service:
        result = asyncio.run(
            Runner.run(
                agent,
                'Complète ma semaine',
                context=AgentContext(user=user, conversation_id=0),
                run_config=RunConfig(tracing_disabled=True),
            )
        )

    assert result.final_output == 'Voilà pour tes soirs libres.'
    meals = service.call_args.args[1]
    assert (meals[0].recipe_id, meals[0].date, meals[0].reason) == (12, '2026-11-16', 'ton classique')
    tool_msg = next(m for m in calls[1]['messages'] if m.get('role') == 'tool')
    assert "card_type='week_meal_proposals'" in tool_msg['content']
