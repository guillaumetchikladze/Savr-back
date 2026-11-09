"""Sous-recettes « simplifié / compliqué » : graphe (boucles, profondeur), liens, courses, plan de bataille, détail."""

from decimal import Decimal

import pytest

from recipes.models import Ingredient, RecipeIngredient, ShoppingListItem, Step
from recipes.services.sub_recipe_service import sub_recipe_hints

pytestmark = pytest.mark.django_db

BASKET_URL = '/api/basket/'


def _line_url(line_id, suffix='sub-recipe'):
    return f'/api/recipe-ingredients/{line_id}/{suffix}/'


def _add(recipe, name, quantity, unit, **extra):
    ingredient, _ = Ingredient.objects.get_or_create(name=name)
    return RecipeIngredient.objects.create(recipe=recipe, ingredient=ingredient, quantity=quantity, unit=unit, **extra)


def _steps(recipe, texts):
    for order, text in enumerate(texts, 1):
        Step.objects.create(recipe=recipe, order=order, instruction=text)


@pytest.fixture
def quiche(user, make_recipe):
    """Quiche (4 parts) : 1 pâte brisée (sa recette, 1 fournée), beurre ; pâte : farine, beurre, beurre noisette."""
    pate = make_recipe(user, 'Pâte brisée', servings=1, prep_time=15, cook_time=0, difficulty='medium')
    _add(pate, 'farine', 250, 'g')
    _add(pate, 'beurre', 125, 'g')
    noisette = make_recipe(user, 'Beurre noisette', servings=1, prep_time=5, cook_time=5, difficulty='hard')
    _add(noisette, 'beurre', 20, 'g')
    nested = _add(pate, 'beurre noisette', 1, 'tbsp', sub_recipe=noisette, default_mode='make')
    recipe = make_recipe(user, 'Quiche lorraine', servings=4, prep_time=20, cook_time=35, difficulty='easy')
    line = _add(recipe, 'pâte brisée', 1, 'piece', sub_recipe=pate)
    _add(recipe, 'beurre', 50, 'g')
    _add(recipe, 'oeufs', 3, 'piece')
    return {'recipe': recipe, 'pate': pate, 'noisette': noisette, 'line': line, 'nested': nested}


def test_link_guards_cycles_and_depth(api, user, make_recipe):
    a, b, c, d, e = (make_recipe(user, t) for t in 'ABCDE')
    client = api.as_user(user)
    a_line = _add(a, 'b', 1, 'piece')
    b_line = _add(b, 'c', 1, 'piece')
    c_line = _add(c, 'd', 1, 'piece')
    d_line = _add(d, 'e', 1, 'piece')

    assert client.put(_line_url(a_line.id), {'sub_recipe_id': a.id}, format='json').data['code'] == 'cycle'
    assert client.put(_line_url(a_line.id), {'sub_recipe_id': b.id}, format='json').status_code == 200
    back = client.put(_line_url(b_line.id), {'sub_recipe_id': a.id}, format='json')  # A → B → A
    assert back.status_code == 400 and back.data['code'] == 'cycle'

    assert client.put(_line_url(b_line.id), {'sub_recipe_id': c.id}, format='json').status_code == 200
    assert client.put(_line_url(c_line.id), {'sub_recipe_id': d.id}, format='json').status_code == 200  # A→B→C→D
    too_deep = client.put(_line_url(d_line.id), {'sub_recipe_id': e.id}, format='json')
    assert too_deep.status_code == 400 and too_deep.data['code'] == 'too_deep'
    # Le niveau du dessus compte aussi : E → A ferait E→A→B→C→D.
    e_line = _add(e, 'a', 1, 'piece')
    assert client.put(_line_url(e_line.id), {'sub_recipe_id': a.id}, format='json').data['code'] == 'too_deep'

    unlinked = client.delete(_line_url(c_line.id))
    assert unlinked.status_code == 200 and unlinked.data['line']['sub_recipe'] is None
    assert client.put(_line_url(d_line.id), {'sub_recipe_id': e.id}, format='json').status_code == 200


def test_link_permissions_and_suggestions(api, user, make_user, make_recipe):
    bob = make_user('bob')
    mine = make_recipe(user, 'Tarte aux pommes')
    line = _add(mine, 'pâte brisée', 1, 'piece')
    bobs_public = make_recipe(bob, 'Pâte brisée')
    bobs_private = make_recipe(bob, 'Pâte brisée au beurre', is_public=False)
    my_pate = make_recipe(user, 'Pâte brisée maison')
    make_recipe(user, 'Gratin dauphinois')
    bobs_tart = make_recipe(bob, 'Tarte de Bob')
    bobs_line = _add(bobs_tart, 'pâte brisée', 1, 'piece')
    bobs_secret = make_recipe(bob, 'Secret', is_public=False)
    secret_line = _add(bobs_secret, 'pâte brisée', 1, 'piece')

    alice = api.as_user(user)
    suggestions = alice.get(_line_url(line.id, 'sub-recipe-suggestions'))
    assert suggestions.status_code == 200
    ids = [s['id'] for s in suggestions.data['suggestions']]
    assert ids[0] == bobs_public.id  # titre identique d'abord
    assert my_pate.id in ids and bobs_private.id not in ids and mine.id not in ids

    assert alice.put(_line_url(bobs_line.id), {'sub_recipe_id': my_pate.id}, format='json').status_code == 403
    assert alice.put(_line_url(secret_line.id), {'sub_recipe_id': my_pate.id}, format='json').status_code == 404
    hidden = alice.put(_line_url(line.id), {'sub_recipe_id': bobs_private.id}, format='json')
    assert hidden.status_code == 404 and hidden.data['code'] == 'sub_recipe_not_found'
    bad_mode = alice.put(_line_url(line.id), {'sub_recipe_id': bobs_public.id, 'default_mode': 'steal'}, format='json')
    assert bad_mode.status_code == 400

    ok = alice.put(
        _line_url(line.id),
        {'sub_recipe_id': bobs_public.id, 'default_mode': 'make', 'sub_recipe_ratio': '0.5'},
        format='json',
    )
    assert ok.status_code == 200, ok.data
    assert ok.data['line']['sub_recipe']['id'] == bobs_public.id
    assert ok.data['line']['default_mode'] == 'make' and ok.data['line']['sub_recipe_ratio'] == '0.500'
    assert [s['line_id'] for s in ok.data['sub_recipes']] == [line.id]


def test_shopping_list_buy_then_make_scaled_merged_recursive(api, user, quiche):
    client = api.as_user(user)
    item = client.post(BASKET_URL, {'recipe_id': quiche['recipe'].id, 'servings': 8}, format='json').data
    res = client.post(f'{BASKET_URL}shopping-list/', {'item_ids': [item['id']]}, format='json')
    assert res.status_code in (200, 201), res.data
    list_id = res.data['shopping_list']['id']

    def totals():
        return {
            (i.ingredient.name, i.unit_group): sum(Decimal(str(q.quantity)) for q in i.quantities.all())
            for i in ShoppingListItem.objects.filter(shopping_list_id=list_id)
        }

    # Défaut « acheter » (×2 pour 8 parts) : la pâte est une ligne comme les autres.
    assert totals() == {
        ('pâte brisée', 'count'): Decimal('2'),
        ('beurre', 'weight'): Decimal('100'),
        ('oeufs', 'count'): Decimal('6'),
    }

    # « Faire maison » : la pâte est remplacée par sa recette ×2, beurre fusionné ; le beurre noisette (défaut
    # « make ») est lui-même remplacé par sa recette. La liste déjà liée est recalculée.
    line = quiche['line']
    made = client.post(f'{BASKET_URL}{item["id"]}/sub-recipe-modes/', {'modes': {str(line.id): 'make'}}, format='json')
    assert made.status_code == 200, made.data
    assert made.data['sub_recipe_modes'] == {str(line.id): 'make'}
    assert made.data['sub_recipes'][0]['mode'] == 'make'
    assert totals() == {
        ('farine', 'weight'): Decimal('500'),
        ('beurre', 'weight'): Decimal('100') + Decimal('250') + Decimal('40'),
        ('oeufs', 'count'): Decimal('6'),
    }

    # Le beurre noisette acheté, puis retour à « acheter » pour la pâte.
    nested = str(quiche['nested'].id)
    client.post(f'{BASKET_URL}{item["id"]}/sub-recipe-modes/', {'modes': {nested: 'buy'}}, format='json')
    assert totals()[('beurre noisette', 'other')] == Decimal('2')
    client.post(f'{BASKET_URL}{item["id"]}/sub-recipe-modes/', {'modes': {str(line.id): 'buy'}}, format='json')
    assert totals() == {
        ('pâte brisée', 'count'): Decimal('2'),
        ('beurre', 'weight'): Decimal('100'),
        ('oeufs', 'count'): Decimal('6'),
    }
    # Une ligne hors de l'arbre de la recette est refusée.
    other = client.post(f'{BASKET_URL}{item["id"]}/sub-recipe-modes/', {'modes': {'999999': 'make'}}, format='json')
    assert other.status_code == 400 and other.data['code'] == 'unknown_line'


def test_battle_plan_sub_recipe_finishes_before_the_step_that_uses_it(api, user, quiche):
    _steps(
        quiche['recipe'],
        [
            'Préchauffer le four à 180°C.',
            'Battre les oeufs avec la crème.',
            'Garnir la pâte brisée avec l’appareil.',
            'Enfourner 35 min à 180°C.',
        ],
    )
    _steps(quiche['pate'], ['Sabler la farine et le beurre.', 'Laisser reposer 30 min au frais.'])
    _steps(quiche['noisette'], ['Faire fondre le beurre jusqu’à coloration noisette 5 min en remuant.'])
    client = api.as_user(user)
    recipe_id, pate_id = quiche['recipe'].id, quiche['pate'].id

    bought = client.post('/api/battle-plan/', {'recipe_ids': [recipe_id]}, format='json').data
    assert [r['id'] for r in bought['recipes']] == [recipe_id]

    res = client.post(
        '/api/battle-plan/',
        {'recipe_ids': [recipe_id], 'sub_recipe_modes': {str(quiche['line'].id): 'make'}},
        format='json',
    )
    assert res.status_code == 200, res.data
    plan = res.data
    roles = {r['id']: (r['role'], r['parent_recipe_id']) for r in plan['recipes']}
    assert roles == {
        quiche['noisette'].id: ('sub_recipe', pate_id),
        pate_id: ('sub_recipe', recipe_id),
        recipe_id: ('main', None),
    }
    garnish = next(s for s in plan['steps'] if s['recipe_id'] == recipe_id and 'Garnir' in s['label'])
    assert garnish['waits_for_recipe_ids'] == [pate_id]
    pate_end = max(s['end_offset_min'] for s in plan['steps'] if s['recipe_id'] == pate_id)
    noisette_end = max(s['end_offset_min'] for s in plan['steps'] if s['recipe_id'] == quiche['noisette'].id)
    pate_start = min(s['start_offset_min'] for s in plan['steps'] if s['recipe_id'] == pate_id)
    assert pate_end <= garnish['start_offset_min']
    assert noisette_end <= pate_start
    assert any('Pâte brisée maison' in line for line in plan['summary'])


def test_recipe_detail_exposes_sub_recipes_and_edit_keeps_links(api, user, make_recipe, quiche):
    client = api.as_user(user)
    detail = client.get(f'/api/recipes/{quiche["recipe"].id}/').data
    [sub] = detail['sub_recipes']
    assert sub['line_id'] == quiche['line'].id and sub['sub_recipe']['title'] == 'Pâte brisée'
    assert sub['default_mode'] == 'buy' and sub['mode'] == 'buy'
    # Pâte 15 min + beurre noisette 10 min (fait maison dans la « version maison »).
    assert sub['extra_time_min'] == 25 and sub['extra_time_label'] == '+ 25 min si tu la fais'
    assert detail['total_time_if_made'] == 55 + 25
    assert detail['difficulty_if_made'] == 'hard'

    plain = client.get(f'/api/recipes/{make_recipe(user, "Salade").id}/').data
    assert plain['sub_recipes'] == [] and plain['total_time_if_made'] is None

    lines = client.get(f'/api/recipes/{quiche["recipe"].id}/ingredients/').data
    linked = next(row for row in lines if row['id'] == quiche['line'].id)
    assert linked['sub_recipe']['id'] == quiche['pate'].id and linked['default_mode'] == 'buy'

    # L'édition remplace les ingrédients : la ligne liée garde son id et son lien.
    edit = client.put(
        f'/api/recipes/{quiche["recipe"].id}/edit/',
        {
            'ingredients': [
                {'ingredient_name': 'pâte brisée', 'quantity': 2, 'unit': 'piece'},
                {'ingredient_name': 'crème', 'quantity': 200, 'unit': 'ml'},
            ]
        },
        format='json',
    )
    assert edit.status_code == 200, edit.data
    kept = RecipeIngredient.objects.get(recipe=quiche['recipe'], ingredient__name='pâte brisée')
    assert kept.id == quiche['line'].id and kept.sub_recipe_id == quiche['pate'].id and kept.quantity == 2
    assert set(quiche['recipe'].recipe_ingredients.values_list('ingredient__name', flat=True)) == {
        'pâte brisée',
        'crème',
    }

    # Chat : les recherches exposent les sous-recettes.
    [hint] = sub_recipe_hints([quiche['recipe'].id])[quiche['recipe'].id]
    assert hint['sub_recipe_id'] == quiche['pate'].id
    assert hint['hint'] == 'pâte brisée : tu peux l’acheter ou la faire maison (+ 15 min si tu la fais).'
