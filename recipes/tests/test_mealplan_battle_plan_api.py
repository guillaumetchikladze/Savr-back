"""« Plan de bataille » : ordonnancement multi-recettes déterministe, endpoint, accès, reformulation LLM (mockée)."""

from datetime import datetime, timedelta
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import override_settings

from recipes.models import MealInvitation, RecipeIngredient, Step
from recipes.services import basket_service
from recipes.services import battle_plan as bp

pytestmark = pytest.mark.django_db

URL = '/api/battle-plan/'

CAKE_STEPS = [
    'Préchauffer le four à 180°C.',
    'Mélanger le chocolat fondu, le beurre, le sucre et les oeufs.',
    'Enfourner 40 min à 180°C.',
    'Sortir du four et laisser refroidir 1 h.',
]
RISOTTO_STEPS = [
    "Émincer l'oignon.",
    "Faire revenir l'oignon 5 min dans le beurre.",
    'Ajouter le riz puis le bouillon louche par louche en remuant pendant 18 min.',
    'Hors du feu, ajouter le parmesan et servir.',
]


@pytest.fixture
def recipe_with_steps(make_recipe):
    from recipes.models import Ingredient

    def _make(owner, title, steps, *, ingredients=(), timers=None, **extra):
        recipe = make_recipe(owner, title, **extra)
        for order, text in enumerate(steps, 1):
            minutes = (timers or {}).get(order)
            Step.objects.create(
                recipe=recipe, order=order, instruction=text, has_timer=bool(minutes), timer_duration=minutes
            )
        for name, qty, unit in ingredients:
            ing, _ = Ingredient.objects.get_or_create(name=name)
            RecipeIngredient.objects.create(recipe=recipe, ingredient=ing, quantity=qty, unit=unit)
        return recipe

    return _make


def _steps_of(plan, recipe_id):
    return [s for s in plan['steps'] if s['recipe_id'] == recipe_id]


def test_long_bake_and_cooling_start_first_and_risotto_fits_inside(api, user, recipe_with_steps):
    risotto = recipe_with_steps(user, 'Risotto', RISOTTO_STEPS, prep_time=10, cook_time=25, timers={3: 18})
    cake = recipe_with_steps(user, 'Gâteau au chocolat', CAKE_STEPS, prep_time=20, cook_time=40)

    res = api.as_user(user).post(URL, {'recipe_ids': [risotto.id, cake.id]}, format='json')

    assert res.status_code == 200, res.data
    plan = res.data
    assert plan['steps'][0]['recipe_id'] == cake.id  # le gâteau doit cuire puis refroidir : il part en premier
    assert (
        plan['total_duration_min'] < plan['sequential_duration_min'] == sum(r['duration_min'] for r in plan['recipes'])
    )
    assert plan['summary'][0] == 'Lance d’abord Gâteau au chocolat : il doit refroidir (1 h).'
    assert 'au lieu de' in plan['summary'][-1]

    bake, cooling = [s for s in _steps_of(plan, cake.id) if not s['active'] and s['kind'] in ('bake', 'cool')]
    assert bake['resource'] == 'oven' and bake['temperature_c'] == 180 and cooling['resource'] is None
    stirring = next(s for s in _steps_of(plan, risotto.id) if s['step_order'] == 3)
    assert stirring['active'] and stirring['duration_min'] == 18 and not stirring['estimated']
    # Tout le risotto (actif) se fait pendant que le gâteau cuit / refroidit, et finit en même temps.
    assert all(s['start_offset_min'] >= bake['start_offset_min'] for s in _steps_of(plan, risotto.id))
    assert max(s['end_offset_min'] for s in _steps_of(plan, risotto.id)) == plan['total_duration_min']
    # Un seul cuisinier : les étapes actives ne se chevauchent jamais.
    active = sorted((s['start_offset_min'], s['end_offset_min']) for s in plan['steps'] if s['active'])
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(active, active[1:], strict=False))
    # Le préchauffage tombe juste avant la cuisson.
    preheat = next(s for s in plan['steps'] if s['kind'] == 'preheat')
    assert preheat['end_offset_min'] == bake['start_offset_min']


def test_oven_temperature_conflict_is_flagged_and_serialized():
    gratin = bp.RecipeInput(1, 'Gratin', steps=[bp.StepInput(11, 1, '', 'Enfourner 1 h à 200 °C.')])
    tart = bp.RecipeInput(2, 'Tarte', steps=[bp.StepInput(21, 1, '', 'Cuire au four 30 min, th. 6.')])
    same_temp = bp.RecipeInput(3, 'Légumes rôtis', steps=[bp.StepInput(31, 1, '', 'Enfourner 30 min à 200°C.')])

    plan = bp.build_plan([gratin, tart, same_temp])

    assert len(plan['conflicts']) == 2  # la tarte (180 °C) contre chacun des plats à 200 °C
    for conflict in plan['conflicts']:
        assert conflict['type'] == 'oven_temperature' and 2 in conflict['recipe_ids']
        assert sorted(conflict['temperatures']) == [180, 200]
        assert conflict['message'].startswith('Un seul four') and conflict['message'] in plan['summary']
    by_recipe = {s['recipe_id']: s for s in plan['steps']}
    g, t, v = by_recipe[1], by_recipe[2], by_recipe[3]
    assert t['end_offset_min'] <= g['start_offset_min'] or g['end_offset_min'] <= t['start_offset_min']
    # Même température : les deux plats partagent le four.
    assert v['start_offset_min'] < g['end_offset_min'] and g['start_offset_min'] < v['end_offset_min']
    assert plan['total_duration_min'] == 90


def test_overnight_marinade_goes_to_the_day_before(api, user, recipe_with_steps):
    chicken = recipe_with_steps(
        user,
        'Poulet tandoori',
        [
            'Mélanger le yaourt et les épices, enrober le poulet.',
            'Laisser mariner une nuit au réfrigérateur.',
            'Cuire au four 25 min à 220°C.',
        ],
        prep_time=15,
        cook_time=25,
    )
    rice = recipe_with_steps(user, 'Riz basmati', ['Rincer le riz.', 'Cuire le riz 12 min à couvert.'])

    res = api.as_user(user).post(URL, {'recipe_ids': [chicken.id, rice.id]}, format='json')

    assert res.status_code == 200, res.data
    plan = res.data
    assert [s['step_order'] for s in plan['day_before']] == [1, 2]
    assert all(s['day'] == 'day_before' and s['recipe_id'] == chicken.id for s in plan['day_before'])
    assert plan['day_before'][1]['kind'] == 'marinate' and not plan['day_before'][1]['active']
    assert {(s['recipe_id'], s['step_order']) for s in plan['steps']} >= {(chicken.id, 3)}
    assert plan['total_duration_min'] < 60  # la nuit de marinade ne compte pas dans le jour J
    assert plan['summary'][0] == 'La veille : mets à mariner Poulet tandoori.'


def test_target_ready_time_back_computes_start_and_meal_plan_hint(
    api, user, make_meal_plan, recipe_with_steps, make_recipe
):
    cake = recipe_with_steps(user, 'Gâteau', CAKE_STEPS, prep_time=20, cook_time=40)
    risotto = recipe_with_steps(user, 'Risotto', RISOTTO_STEPS, prep_time=10, cook_time=25)
    meal = make_meal_plan(user, meal_time='dinner', recipes=[cake, risotto])
    solo = make_meal_plan(user, meal_time='lunch', recipes=[make_recipe(user)])

    res = api.as_user(user).post(URL, {'meal_plan_id': meal.id, 'target_ready_time': '20:00'}, format='json')

    assert res.status_code == 200, res.data
    plan = res.data
    ready, start = datetime.fromisoformat(plan['ready_at']), datetime.fromisoformat(plan['start_at'])
    assert ready.date() == meal.date and (ready.hour, ready.minute) == (20, 0)
    assert ready - start == timedelta(minutes=plan['total_duration_min'])
    assert plan['ready_label'] == '20 h'
    assert plan['headline'] == f'Commence à {plan["start_label"]} pour manger à 20 h'
    assert plan['steps'][0]['start_at'] == plan['start_at']
    assert plan['source'] == {'type': 'meal_plan', 'meal_plan_id': meal.id}

    # Heure du repas déjà réglée → horaire par défaut ; start_time → heure de fin calculée.
    meal.scheduled_time = datetime(2026, 1, 1, 19, 30).time()
    meal.save(update_fields=['scheduled_time'])
    assert api.post(URL, {'meal_plan_id': meal.id}, format='json').data['ready_label'] == '19 h 30'
    started = api.post(URL, {'recipe_ids': [cake.id], 'start_time': '18h'}, format='json').data
    assert started['start_label'] == '18 h' and started['ready_at'] is not None

    # Indice sur le repas (≥ 2 recettes), sans requête dédiée.
    assert api.get(f'/api/meal-plans/{meal.id}/').data['has_battle_plan'] is True
    assert api.get(f'/api/meal-plans/{solo.id}/').data['has_battle_plan'] is False


def test_basket_batch_cooking_groups_shared_prep(api, user, recipe_with_steps):
    risotto = recipe_with_steps(
        user,
        'Risotto',
        RISOTTO_STEPS,
        servings=2,
        ingredients=[('oignon', 1, 'piece'), ('sel', 1, 'pinch'), ('parmesan', 50, 'g')],
    )
    quiche = recipe_with_steps(
        user,
        'Quiche',
        ['Émincer finement les oignons et les faire fondre.', 'Garnir la pâte et enfourner 35 min à 180°C.'],
        servings=4,
        ingredients=[('oignon', 2, 'piece'), ('sel', 1, 'pinch'), ('lardons', 200, 'g')],
    )
    items = [
        basket_service.add_item(user, recipe_id=risotto.id, servings=2),
        basket_service.add_item(user, recipe_id=quiche.id, servings=4),
        basket_service.add_item(user, label='Soupe de potiron'),
    ]

    res = api.as_user(user).post(URL, {'basket_item_ids': [i.id for i in items]}, format='json')

    assert res.status_code == 200, res.data
    plan = res.data
    (prep,) = plan['shared_prep']  # sel = basique, parmesan / lardons dans une seule recette
    assert prep['label'] == 'Émince 3 oignons pour Risotto et Quiche'
    assert prep['totals'] == [{'quantity': 3.0, 'unit': 'piece'}] and set(prep['recipe_ids']) == {risotto.id, quiche.id}
    assert plan['skipped'] == [{'basket_item_id': items[2].id, 'label': 'Soupe de potiron', 'reason': 'no_recipe'}]
    assert any(line.startswith('Prépa commune') for line in plan['summary'])

    # Portions doublées → quantités doublées.
    doubled = api.post(URL, {'basket_item_ids': [items[0].id, items[1].id], 'servings': 8}, format='json').data
    assert doubled['shared_prep'][0]['label'] == 'Émince 8 oignons pour Risotto et Quiche'


def test_access_rules(api, user, make_user, make_recipe, make_meal_plan):
    other = make_user('bob')
    private = make_recipe(other, 'Secret', is_public=False)
    public = make_recipe(other, 'Public')
    host_meal = make_meal_plan(other, recipes=[private, public])
    their_item = basket_service.add_item(other, recipe_id=public.id)
    client = api.as_user(user)

    assert client.post(URL, {'recipe_ids': [public.id, private.id]}, format='json').status_code == 404
    assert client.post(URL, {'meal_plan_id': host_meal.id}, format='json').status_code == 404
    assert client.post(URL, {'basket_item_ids': [their_item.id]}, format='json').status_code == 404
    bad = client.post(URL, {'recipe_ids': [public.id], 'meal_plan_id': host_meal.id}, format='json')
    assert bad.status_code == 400 and bad.data['code'] == 'invalid_source'
    assert client.post(URL, {}, format='json').status_code == 400

    # Invité au repas : il voit le repas et ses recettes privées.
    MealInvitation.objects.create(inviter=other, invitee=user, meal_plan=host_meal, status='accepted')
    assert client.post(URL, {'meal_plan_id': host_meal.id}, format='json').status_code == 200
    assert client.post(URL, {'recipe_ids': [private.id]}, format='json').status_code == 200
    api.force_authenticate(user=None)
    assert api.post(URL, {'recipe_ids': [public.id]}, format='json').status_code == 401


def test_llm_polish_is_off_by_default_rewords_without_reordering_and_falls_back(api, user, recipe_with_steps):
    from recipes.services import battle_plan_polish

    cake = recipe_with_steps(user, 'Gâteau', CAKE_STEPS, prep_time=20, cook_time=40)
    risotto = recipe_with_steps(user, 'Risotto', RISOTTO_STEPS)
    body = {'recipe_ids': [cake.id, risotto.id]}
    client = api.as_user(user)
    cache.clear()

    with mock.patch.object(battle_plan_polish, '_call_llm') as llm:
        plain = client.post(URL, body, format='json').data
    llm.assert_not_called()
    assert plain['polished'] is False

    def reword(system_prompt, prompt):
        import json

        texts = json.loads(prompt)['texts']
        return json.dumps({'texts': [f'Allez : {t}' for t in texts]})

    with override_settings(BATTLE_PLAN_LLM_ENABLED=True):
        with mock.patch.object(battle_plan_polish, '_call_llm', side_effect=reword) as llm:
            polished = client.post(URL, body, format='json').data
            cached = client.post(URL, body, format='json').data
        assert llm.call_count == 1  # 2e appel servi par le cache
        assert polished['polished'] is True and cached == polished
        assert [s['label'] for s in polished['steps']] == [f'Allez : {s["label"]}' for s in plain['steps']]
        strip = ('label',)
        assert [{k: v for k, v in s.items() if k not in strip} for s in polished['steps']] == [
            {k: v for k, v in s.items() if k not in strip} for s in plain['steps']
        ]
        assert polished['summary'][-1] == plain['summary'][-1]  # horaire jamais reformulé

        cache.clear()
        for failure in (TimeoutError('slow'), '{"texts": ["un seul"]}', '{"texts": null}'):
            kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
            with mock.patch.object(battle_plan_polish, '_call_llm', **kwargs):
                fallback = client.post(URL, body, format='json').data
            assert fallback == plain  # repli : texte déterministe, identique

        # Un chiffre inventé (durée, température) → refusé.
        def invent(system_prompt, prompt):
            import json

            return json.dumps({'texts': [f'{t} 999' for t in json.loads(prompt)['texts']]})

        with mock.patch.object(battle_plan_polish, '_call_llm', side_effect=invent):
            assert client.post(URL, body, format='json').data['polished'] is False


def test_step_text_parsing():
    assert bp.parse_duration_minutes('Cuire 1 h 30') == 90
    assert bp.parse_duration_minutes('laisser reposer 20 à 25 min') == 25
    assert bp.parse_duration_minutes('une demi-heure') == 30
    assert bp.parse_duration_minutes('Préchauffer à 180°C') is None
    assert bp.parse_temperature('Cuire th. 7') == 210
    simmer = bp.analyze_step('', 'Laisser mijoter 45 min en remuant de temps en temps.')
    assert not simmer.active and simmer.kind == 'simmer'
    saute = bp.analyze_step('', 'Faire sauter les légumes 8 min à feu vif.')
    assert saute.active and saute.resource == 'hob'
