"""Agent v2 : profil injecté, tools panier / « J'ai cuisiné » / courses, registre et format tool calling (LLM mocké)."""

import asyncio
from datetime import date, timedelta
from unittest import mock

import pytest
from django.test import override_settings
from django.utils import timezone

from accounts.models import HouseholdMember
from chat.services import kitchen_tools
from chat.services.seasonality import in_season_produce, season_for
from chat.services.session_context import MAX_PROFILE_CHARS, build_profile_context, build_session_context_prompt
from chat.services.tool_schemas import PlanWeekResult
from recipes.models import BasketItem, MealPlan, Post, ShoppingListItem
from recipes.services import basket_service

pytestmark = pytest.mark.django_db

V1_TOOL_NAMES = {
    'search_recipes',
    'get_meal_plans',
    'create_meal_plan_slot',
    'add_recipe_to_meal_plan',
    'generate_recipe_from_idea',
    'import_recipe_from_url',
    'propose_meal_deletion',
    'propose_recipe_revision',
    'send_invitation_proposal',
    'get_shopping_list_items',
    'add_shopping_list_item',
}


def _household(owner, pauline):
    HouseholdMember.objects.create(owner=owner, user=pauline, status=HouseholdMember.STATUS_ACCEPTED)
    HouseholdMember.objects.create(owner=owner, name='Jules', is_child=True, status=HouseholdMember.STATUS_ACCEPTED)


# ---------------------------------------------------------------------------
# Contexte injecté
# ---------------------------------------------------------------------------
def test_profile_context_has_season_preferences_household_basket_and_recent_cooks(make_user, user):
    user.regimes = ['vegetarian']
    user.allergies = ['arachide']
    user.food_dislikes = ['coriandre']
    user.save()
    _household(user, make_user('pauline', first_name='Pauline', allergies=['fruits à coque']))
    item = basket_service.add_item(user, label='Soupe de potiron')
    kitchen_tools.log_cooked_for_user(user, label='Gratin de courge', date='hier')

    today = timezone.localdate()
    ctx = build_profile_context(user, today)

    assert f'Saison : {season_for(today)}' in ctx
    assert 'végétarien' in ctx and 'arachide' in ctx and 'coriandre' in ctx
    assert 'tu cuisines pour 3 : toi, Pauline, Jules (enfant)' in ctx
    assert 'Pauline : allergies fruits à coque' in ctx
    assert f'Soupe de potiron [#{item.id}, plat libre, à faire]' in ctx
    assert 'Gratin de courge' in ctx.split('Cuisiné récemment')[1]
    assert 'Ce mois-ci :' in ctx
    # Table de saison : novembre = automne, courges et potirons.
    assert season_for(date(2026, 11, 9)) == 'automne' and 'potiron' in in_season_produce(date(2026, 11, 9))
    assert season_for(date(2026, 7, 1)) == 'été'


def test_profile_context_stays_within_budget(make_user, user):
    user.allergies = [f'ingrédient allergène très long numéro {i}' for i in range(20)]
    user.food_dislikes = [f'aliment détesté au nom interminable {i}' for i in range(20)]
    user.save()
    for i in range(15):
        basket_service.add_item(user, label=f'Plat libre au nom vraiment très très long pour tester {i}')
    for i in range(12):
        kitchen_tools.log_cooked_for_user(
            user,
            label=f'Plat cuisiné au nom vraiment très très long {i}',
            date=(date.today() - timedelta(days=i)).isoformat(),
        )

    profile = build_profile_context(user)
    assert len(profile) <= MAX_PROFILE_CHARS
    assert '(+5 autres)' in profile  # panier plafonné à 10 envies
    full = build_session_context_prompt(user)
    assert len(full) < MAX_PROFILE_CHARS + 1500  # ≈ 1 000 tokens au pire, dates comprises


# ---------------------------------------------------------------------------
# plan_week / panier
# ---------------------------------------------------------------------------
def test_plan_week_adds_undated_basket_items_then_shopping_list(user, make_recipe):
    recipe = make_recipe(user, 'Dahl de lentilles', ingredients=['lentilles corail', 'lait de coco'])

    result = kitchen_tools.plan_week_for_user(user, recipe_ids=[recipe.id], labels=['Soupe de potiron'])

    week = basket_service.current_week_start(user)
    assert result.week_start == week.isoformat()
    assert [a.display_title for a in result.added] == ['Dahl de lentilles', 'Soupe de potiron']
    items = BasketItem.objects.filter(user=user, week_start=week)
    assert {i.id for i in items} == {a.basket_item_id for a in result.added}
    assert all(i.meal_plan_id is None for i in items)  # sans date par défaut
    assert result.added[0].undo.path == f'/api/basket/{result.added[0].basket_item_id}/'
    assert result.message.startswith('2 plat(s) ajouté(s) au panier')

    shopping = kitchen_tools.basket_to_shopping_list(user)
    assert shopping.created_list and shopping.added_basket_item_ids == [result.added[0].basket_item_id]
    names = set(
        ShoppingListItem.objects.filter(shopping_list_id=shopping.shopping_list_id).values_list(
            'ingredient__name', flat=True
        )
    )
    assert names == {'lentilles corail', 'lait de coco'}
    assert 'plat(s) libre(s)' in shopping.message  # la soupe sans recette est ignorée

    removed = kitchen_tools.remove_basket_items(user, [result.added[1].basket_item_id])
    assert removed.removed_basket_item_ids == [result.added[1].basket_item_id]
    assert BasketItem.objects.filter(user=user).count() == 1


def test_plan_week_with_a_date_schedules_a_real_meal(user):
    day = basket_service.user_today(user) + timedelta(days=1)
    result = kitchen_tools.plan_week_for_user(
        user, labels=['Tarte aux poireaux'], date=day.isoformat(), meal_time='midi'
    )

    added = result.added[0]
    assert (added.status, added.date, added.meal_time) == ('planned', day.isoformat(), 'lunch')
    assert MealPlan.objects.get(id=added.meal_plan_id).confirmed is False


def test_plan_week_respects_preferences_and_household_allergies(make_user, user, make_recipe):
    user.food_dislikes = ['coriandre']
    user.save()
    _household(user, make_user('pauline', first_name='Pauline', allergies=['arachide']))
    satay = make_recipe(user, 'Brochettes satay', ingredients=['poulet', 'arachide'])

    result = kitchen_tools.plan_week_for_user(
        user, recipe_ids=[satay.id], labels=['Curry à la coriandre', 'Risotto de potiron']
    )

    assert [a.display_title for a in result.added] == ['Risotto de potiron']
    skipped = {s.label: s for s in result.skipped}
    assert skipped['Brochettes satay'].reason == 'allergy' and 'Pauline' in skipped['Brochettes satay'].detail
    assert skipped['Curry à la coriandre'].reason == 'dislike'

    forced = kitchen_tools.plan_week_for_user(user, labels=['Curry à la coriandre'], allow_conflicts=True)
    assert [a.display_title for a in forced.added] == ['Curry à la coriandre']


# ---------------------------------------------------------------------------
# log_cooked
# ---------------------------------------------------------------------------
def test_log_cooked_goes_through_cooked_service_with_ardoise_and_badges(user):
    item = basket_service.add_item(user, label='Soupe de potiron')

    result = kitchen_tools.log_cooked_for_user(user, label='Soupe de potiron')

    post = Post.objects.get(id=result.post_id)
    assert post.meal_plan.confirmed and post.meal_plan_id == result.meal_plan_id
    assert result.is_published is False and post.is_published is False  # auto_publish_cooked=False par défaut
    assert result.basket_item_id == item.id  # l'envie non datée de la semaine est cochée
    assert result.highlights and all({'kind', 'label'} <= set(h) for h in result.highlights)
    assert result.new_badges  # premier « cuisiné » → au moins un badge
    assert result.undo.path == f'/api/posts/{post.id}/?undo_cooked=true'

    again = kitchen_tools.log_cooked_for_user(user, label='Soupe de potiron')
    assert (again.created, again.post_id, again.new_badges) == (False, post.id, [])

    assert f'[post {post.id}]' in build_profile_context(user)  # « annule » possible aux tours suivants
    kitchen_tools.undo_cooked_for_user(user)  # sans id : le dernier « J'ai cuisiné »
    assert not Post.objects.filter(id=post.id).exists()
    item.refresh_from_db()
    assert item.meal_plan_id is None  # l'envie redevient « à faire »


def test_log_cooked_yesterday_publishes_per_user_setting_and_refuses_the_future(user):
    user.auto_publish_cooked = True
    user.save()

    result = kitchen_tools.log_cooked_for_user(user, label='Pâtes au pesto', date='hier', meal_time='soir')

    yesterday = timezone.localdate() - timedelta(days=1)
    assert (result.date, result.meal_time, result.is_published) == (yesterday.isoformat(), 'dinner', True)
    with pytest.raises(ValueError, match='plan_week'):
        kitchen_tools.log_cooked_for_user(user, label='Raclette', date='demain')


# ---------------------------------------------------------------------------
# Registre, modèle, format tool calling (client OpenAI-compatible mocké)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_api_key():
    with mock.patch('chat.services.agent_factory._api_key', return_value='test-key'):
        yield


def test_agent_keeps_v1_tools_adds_v2_tools_and_uses_gemini_25_by_default(user, fake_api_key):
    from chat.services.agent_factory import chat_model_name, create_planning_agent

    agent = create_planning_agent(user, session_context='Contexte de test')
    names = {t.name for t in agent.tools}
    assert V1_TOOL_NAMES <= names
    assert {'plan_week', 'log_cooked', 'add_to_shopping_list_from_basket'} <= names
    assert agent.model.model == chat_model_name() == 'gemini-2.5-flash'
    assert agent.instructions.endswith('Contexte de test')
    with override_settings(CHAT_AI_MODEL='models/gemini-2.5-pro'):
        assert create_planning_agent().model.model == 'gemini-2.5-pro'


def _completion(message: dict, finish_reason: str):
    from openai.types.chat import ChatCompletion

    return ChatCompletion.model_validate(
        {
            'id': 'cmpl-test',
            'object': 'chat.completion',
            'created': 0,
            'model': 'gemini-2.5-flash',
            'choices': [{'index': 0, 'finish_reason': finish_reason, 'message': message}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        }
    )


def test_tool_call_round_trip_through_openai_compatible_client(user, fake_api_key):
    """
    Le modèle demande plan_week (JSON à la Gemini, guillemets simples) → le tool est appelé avec les bons
    arguments → 2e appel avec son résultat. Le service est mocké : les écritures sont testées plus haut
    (pas de DB depuis la boucle async, hors transaction de test).
    """
    from agents import RunConfig, Runner

    from chat.services.agent_context import AgentContext
    from chat.services.agent_factory import create_planning_agent

    agent = create_planning_agent(user, session_context='Contexte de test')
    calls = []
    replies = [
        _completion(
            {
                'role': 'assistant',
                'content': None,
                'tool_calls': [
                    {
                        'id': 'call_1',
                        'type': 'function',
                        'function': {'name': 'plan_week', 'arguments': "{'labels': ['Soupe de potiron']}"},
                    }
                ],
            },
            'tool_calls',
        ),
        _completion({'role': 'assistant', 'content': "C'est dans ta semaine."}, 'stop'),
    ]

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return replies[len(calls) - 1]

    agent.model._client.chat.completions.create = fake_create
    planned = PlanWeekResult(week_start='2026-11-09', message='1 plat(s) ajouté(s) au panier de la semaine.')
    with mock.patch.object(kitchen_tools, 'plan_week_for_user', return_value=planned) as service:
        result = asyncio.run(
            Runner.run(
                agent,
                'Ajoute une soupe de potiron à ma semaine',
                context=AgentContext(user=user, conversation_id=0),
                run_config=RunConfig(tracing_disabled=True),
            )
        )

    assert result.final_output == "C'est dans ta semaine."
    assert service.call_args.kwargs['labels'] == ['Soupe de potiron']
    first = calls[0]
    assert first['model'] == 'gemini-2.5-flash'
    tools = {t['function']['name']: t['function'] for t in first['tools']}
    assert 'plan_week' in tools and all('strict' not in fn for fn in tools.values())
    assert tools['plan_week']['parameters']['properties']['labels']
    assert not {'reasoning_effort', 'store', 'stream_options', 'metadata'} & set(first)
    tool_msg = next(m for m in calls[1]['messages'] if m.get('role') == 'tool')
    assert "card_type='basket_additions'" in tool_msg['content']  # sortie pydantic sérialisée par le SDK


def test_agent_tracing_disabled_unless_opted_in():
    from agents.tracing import get_trace_provider, set_tracing_disabled

    from chat.services.tracing import apply_tracing_setting

    try:
        set_tracing_disabled(False)
        with override_settings(AGENT_TRACING_ENABLED=False):
            apply_tracing_setting()
        assert get_trace_provider()._disabled is True
        with override_settings(AGENT_TRACING_ENABLED=True):
            apply_tracing_setting()
        assert get_trace_provider()._disabled is False
    finally:
        apply_tracing_setting()
