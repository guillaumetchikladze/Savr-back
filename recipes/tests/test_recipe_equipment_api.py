"""« Ta cuisine » : équipements de l'utilisateur, équipements requis des recettes, recherche, plan de bataille, adaptation."""

import json
from unittest import mock

import pytest
from django.core.management import call_command

from accounts.models import HouseholdMember
from recipes.models import Recipe, Step
from recipes.services import equipment_service
from recipes.services.equipment_inference import infer_equipment

pytestmark = pytest.mark.django_db

EQUIPMENT_URL = '/api/equipment/'
CAKE = ['Préchauffer le four à 180°C.', 'Mélanger le chocolat et les oeufs.', 'Enfourner 30 min.']


@pytest.fixture
def recipe_with_steps(make_recipe):
    def _make(owner, title, steps, **extra):
        recipe = make_recipe(owner, title, ingredients=['farine', 'oeuf'], **extra)
        for order, text in enumerate(steps, 1):
            Step.objects.create(recipe=recipe, order=order, instruction=text)
        recipe.refresh_from_db()
        return recipe

    return _make


def _set_kitchen(user, **data):
    equipment_service.set_equipment(user, data)
    user.refresh_from_db()


# ---------------------------------------------------------------------------
# Catalogue + GET/PUT
# ---------------------------------------------------------------------------
def test_catalog_and_get_put_with_household_fallback(api, user, make_user):
    client = api.as_user(user)
    catalog = client.get(f'{EQUIPMENT_URL}catalog/').data
    keys = [i['key'] for i in catalog['items']]
    assert len(keys) >= 16 and {'four', 'plaques', 'airfryer', 'barbecue_plancha', 'robot_cuiseur'} <= set(keys)
    assert [i['key'] for i in catalog['classic']] == ['four', 'plaques', 'micro_ondes', 'mixeur_plongeant']
    assert next(i for i in catalog['items'] if i['key'] == 'four')['hard'] is True

    res = client.get(EQUIPMENT_URL)
    assert res.data['has_set_equipment'] is False and res.data['source'] == 'default'
    assert res.data['keys'] == ['four', 'plaques', 'micro_ondes', 'mixeur_plongeant']  # pré-sélection « classique »
    assert client.get('/api/auth/profile/').data['has_set_equipment'] is False

    res = client.put(
        EQUIPMENT_URL,
        {'equipment': [{'key': 'airfryer'}, {'key': 'plaques', 'burners': 2, 'induction': True}]},
        format='json',
    )
    assert res.status_code == 200, res.data
    assert res.data['has_set_equipment'] is True and res.data['source'] == 'mine'
    assert res.data['keys'] == ['plaques', 'airfryer']
    assert res.data['equipment'][0] == {
        'key': 'plaques',
        'label': 'Plaques de cuisson',
        'icon': 'flame-outline',
        'emoji': '🔥',
        'burners': 2,
        'induction': True,
    }
    assert client.put(EQUIPMENT_URL, {'keys': ['four', 'teleporteur']}, format='json').data['code'] == (
        'unknown_equipment'
    )
    assert client.put(EQUIPMENT_URL, {'keys': ['plaques'], 'burners': 9}, format='json').status_code == 400

    # Foyer : une personne liée qui n'a rien renseigné partage ma cuisine ; dès qu'elle renseigne la sienne, elle prime.
    partner = make_user('bob')
    HouseholdMember.objects.create(owner=user, user=partner, status=HouseholdMember.STATUS_ACCEPTED)
    res = api.as_user(partner).get(EQUIPMENT_URL)
    assert res.data['source'] == 'household' and res.data['has_set_equipment'] is False
    assert res.data['keys'] == ['plaques', 'airfryer']
    res = api.as_user(partner).put(EQUIPMENT_URL, {'keys': []}, format='json')
    assert res.data['source'] == 'mine' and res.data['keys'] == []


# ---------------------------------------------------------------------------
# Inférence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ('title', 'steps', 'expected'),
    [
        ('Gâteau', CAKE, ['four']),
        ('Ailes de poulet', ['Mélanger les épices.', "Cuire 18 min à l'airfryer à 200 °C."], ['airfryer']),
        ('Brochettes', ['Faire griller 8 min sur la plancha bien chaude.'], ['barbecue_plancha']),
        ('Velouté', ["Faire revenir l'oignon dans une casserole.", 'Mixer finement.'], ['plaques', 'mixeur_plongeant']),
        ('Caramel', ['Chauffer le sucre à 160 °C dans une casserole.'], ['plaques']),  # pas un four
        ('Salade', ['Couper les tomates.', 'Assaisonner.'], []),
    ],
)
def test_inference_on_sample_steps(title, steps, expected):
    keys, alternatives = infer_equipment(title, [('', s) for s in steps], servings=4)
    assert keys == expected
    assert alternatives == ({'four': ['airfryer']} if 'four' in expected else {})


def test_required_equipment_follows_steps_override_and_backfill(user, recipe_with_steps):
    cake = recipe_with_steps(user, 'Gâteau', CAKE, servings=8)
    assert cake.required_equipment == ['four'] and cake.equipment_alternatives == {
        'four': []
    }  # 8 parts : pas d'airfryer

    Step.objects.filter(recipe=cake).delete()
    cake.refresh_from_db()
    assert cake.required_equipment == []
    cake.required_equipment_override = ['gaufrier']
    cake.save()
    cake.refresh_from_db()
    assert cake.required_equipment == ['gaufrier']

    other = recipe_with_steps(user, 'Brochettes', ['Griller sur le barbecue.'])
    Recipe.objects.filter(pk=other.pk).update(required_equipment=None)
    call_command('infer_recipe_equipment', '--only-missing')
    other.refresh_from_db()
    assert other.required_equipment == ['barbecue_plancha']


# ---------------------------------------------------------------------------
# Lecture : détail + recherche
# ---------------------------------------------------------------------------
def test_missing_equipment_in_detail_and_search_excludes_unfeasible_oven_recipes(
    api, user, make_user, recipe_with_steps
):
    chef = make_user('chef')
    small = recipe_with_steps(chef, 'Gâteau fondant', CAKE, servings=2)
    big = recipe_with_steps(chef, 'Gâteau géant', CAKE, servings=10)
    mine = recipe_with_steps(user, 'Gâteau maison', CAKE, servings=10)
    salad = recipe_with_steps(chef, 'Gâteau salé froid', ['Mélanger.'])
    client = api.as_user(user)

    # Cuisine inconnue : rien ne manque, rien n'est filtré.
    assert client.get(f'/api/recipes/{small.id}/').data['missing_equipment'] == []

    def search_ids(params=''):
        res = client.get(f'/api/recipes/search_fuzzy/?q=gateau&page_size=50{params}')
        return {r['id'] for r in res.data['results']}

    assert {small.id, big.id, mine.id, salad.id} <= search_ids()

    _set_kitchen(user, keys=['plaques', 'micro_ondes'])
    detail = client.get(f'/api/recipes/{small.id}/').data
    assert [m['key'] for m in detail['missing_equipment']] == ['four']
    assert detail['missing_equipment'][0]['blocking'] is True
    assert detail['missing_equipment_label'] == 'Il te manque : un four'
    assert detail['can_adapt_equipment'] is True and detail['my_adaptation'] is None
    assert detail['required_equipment'][0]['key'] == 'four'
    # Pas de four : les gâteaux des autres disparaissent de la recherche ; les miens restent ; `equipment=any` = tout.
    found = search_ids()
    assert salad.id in found and mine.id in found and not {small.id, big.id} & found
    assert {small.id, big.id} <= search_ids('&equipment=any')

    # Airfryer sans four : le petit gâteau (2 parts) reste, avec l'alternative ; le géant non.
    _set_kitchen(user, keys=['plaques', 'airfryer'])
    found = search_ids()
    assert small.id in found and big.id not in found
    missing = client.get(f'/api/recipes/{small.id}/').data['missing_equipment'][0]
    assert missing['blocking'] is False and missing['alternatives'] == [{'key': 'airfryer', 'label': 'Airfryer'}]


# ---------------------------------------------------------------------------
# Plan de bataille : vraies ressources
# ---------------------------------------------------------------------------
def test_battle_plan_respects_burner_count_and_uses_the_airfryer(api, user, recipe_with_steps):
    soup = recipe_with_steps(user, 'Soupe', ['Faire mijoter 30 min dans une casserole.'])
    stew = recipe_with_steps(user, 'Ragoût', ['Faire mijoter 40 min dans une cocotte sur le feu.'])
    client = api.as_user(user)

    def hob_spans(plan):
        return sorted((s['start_offset_min'], s['end_offset_min']) for s in plan['steps'] if s['resource'] == 'hob')

    plan = client.post('/api/battle-plan/', {'recipe_ids': [soup.id, stew.id]}, format='json').data
    (a0, a1), (b0, b1) = hob_spans(plan)
    assert b0 < a1 and plan['kitchen']['known'] is False  # cuisine inconnue : feux sans limite, en parallèle

    _set_kitchen(user, equipment=[{'key': 'plaques', 'burners': 1}, {'key': 'airfryer'}])
    plan = client.post('/api/battle-plan/', {'recipe_ids': [soup.id, stew.id]}, format='json').data
    (a0, a1), (b0, b1) = hob_spans(plan)
    assert a1 <= b0  # un seul feu : l'un après l'autre
    assert plan['kitchen']['burners'] == 1 and plan['total_duration_min'] == 70

    cake = recipe_with_steps(user, 'Gâteau', CAKE, servings=2)
    plan = client.post('/api/battle-plan/', {'recipe_ids': [cake.id, soup.id]}, format='json').data
    bake = next(s for s in plan['steps'] if s['kind'] == 'bake')
    assert bake['resource'] == 'oven' and bake['alternative_resource'] == 'airfryer'
    assert bake['note'].startswith('À l’airfryer')
    assert plan['equipment_warnings'][0]['type'] == 'oven_to_airfryer'
    assert 'Pas de four : Gâteau passe à l’airfryer' in plan['summary'][-2]


# ---------------------------------------------------------------------------
# « Adapter pour chez moi »
# ---------------------------------------------------------------------------
def _llm_answer(**extra):
    payload = {
        'title': "Gâteau fondant à l'airfryer",
        'note': "Cuit à l'airfryer au lieu du four, en une fournée.",
        'prep_time': 15,
        'cook_time': 25,
        'steps': [
            {'title': 'Préchauffe', 'instruction': "Préchauffe l'airfryer à 160 °C.", 'tip': '', 'timer_duration': 3},
            {'title': 'Mélange', 'instruction': 'Mélange le chocolat et les oeufs.', 'tip': ''},
            {'title': 'Cuisson', 'instruction': "Cuis 25 min à l'airfryer dans un petit moule.", 'timer_duration': 25},
        ],
        **extra,
    }
    return json.dumps(payload, ensure_ascii=False)


def test_adapt_endpoint_is_idempotent_linked_private_and_handles_llm_failure(api, user, make_user, recipe_with_steps):
    chef = make_user('chef')
    cake = recipe_with_steps(chef, 'Gâteau fondant', CAKE, servings=2)
    client = api.as_user(user)
    url = f'/api/recipes/{cake.id}/adapt-equipment/'
    target = 'recipes.services.equipment_adaptation._call_llm'

    assert client.post(url).data['code'] == 'equipment_not_set'
    _set_kitchen(user, keys=['plaques', 'airfryer'])

    with mock.patch(target, side_effect=TimeoutError('timeout')):
        res = client.post(url)
    assert res.status_code == 502 and res.data['code'] == 'adaptation_failed'
    with mock.patch(target, return_value='{"steps": []}'):
        assert client.post(url).status_code == 502
    assert not Recipe.objects.filter(adapted_from=cake).exists()

    with mock.patch(target, return_value=_llm_answer()) as llm:
        first = client.post(url)
        second = client.post(url)
    assert first.status_code == 201 and second.status_code == 200, (first.data, second.data)
    assert llm.call_count == 1  # la 2e demande renvoie l'adaptation existante sans LLM
    prompt = json.loads(llm.call_args.args[1])
    assert prompt['missing'] == ['Four'] and 'Airfryer' in prompt['available']
    copy = Recipe.objects.get(adapted_from=cake)
    assert first.data['recipe']['id'] == second.data['recipe']['id'] == copy.id
    assert first.data['created'] is True and second.data['created'] is False
    assert copy.created_by_id == user.id and copy.is_public is False
    assert copy.adapted_for_equipment == ['plaques', 'airfryer'] and copy.required_equipment == ['airfryer']
    assert copy.title == "Gâteau fondant à l'airfryer" and copy.steps.count() == 3
    assert sorted(copy.recipe_ingredients.values_list('ingredient__name', flat=True)) == ['farine', 'oeuf']
    assert first.data['recipe']['adapted_from'] == {'id': cake.id, 'title': 'Gâteau fondant'}
    assert first.data['recipe']['missing_equipment'] == []
    assert client.get(f'/api/recipes/{cake.id}/').data['my_adaptation'] == {'id': copy.id, 'title': copy.title}
    # Le chef ne voit pas la copie privée ; rien à adapter pour une recette déjà faisable.
    assert api.as_user(chef).get(f'/api/recipes/{copy.id}/').status_code == 404
    assert api.as_user(chef).post(f'/api/recipes/{copy.id}/adapt-equipment/').status_code == 404
    client = api.as_user(user)
    assert client.post(f'/api/recipes/{copy.id}/adapt-equipment/').data['code'] == 'nothing_to_adapt'


def test_ai_pipeline_hints_are_merged_with_inference(user):
    from recipes.services.formalization_pipeline import create_recipe_from_formalized
    from recipes.services.pydantic_models import RecipeFormalized

    formalized = RecipeFormalized(
        title='Risotto express',
        steps_summary='Tout au robot.',
        prep_time=5,
        cook_time=20,
        steps=[{'order': 1, 'instruction': 'Faire revenir le riz dans une casserole.'}],
        required_equipment=['robot_cuiseur', 'teleporteur'],
    )
    recipe = create_recipe_from_formalized(formalized, {}, user)
    recipe.refresh_from_db()
    assert recipe.equipment_ai_hints == ['robot_cuiseur']  # clés hors catalogue ignorées
    assert recipe.required_equipment == ['plaques', 'robot_cuiseur']
