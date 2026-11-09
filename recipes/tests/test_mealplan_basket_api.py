"""Panier « Cette semaine » : envies non datées, datation sans conflit, cuisiné (stats), courses, report."""

from datetime import timedelta
from decimal import Decimal

import pytest

from recipes.models import BasketItem, MealPlan, RecipeBatch, ShoppingListBatch, ShoppingListItem
from recipes.services.basket_service import current_week_start
from recipes.services.cooked_service import cooked_meal_plans

pytestmark = pytest.mark.django_db

BASKET_URL = '/api/basket/'


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


def _add(client, **data):
    resp = client.post(BASKET_URL, data, format='json')
    assert resp.status_code == 201, resp.data
    return resp.data


def _statuses(client, **params):
    return {i['display_title']: i['status'] for i in client.get(BASKET_URL, params).data['items']}


def test_undated_items_then_scheduled_without_slot_conflict(api, user, make_recipe, make_meal_plan):
    client = api.as_user(user)
    risotto = make_recipe(user, 'Risotto')
    week = current_week_start(user)
    tuesday = week + timedelta(days=1)
    make_meal_plan(user, day=tuesday, meal_time='dinner', custom_label='Pizza')  # créneau déjà pris

    a = _add(client, recipe_id=risotto.id, servings=4)
    b = _add(client, label='Quiche')
    c = _add(client, recipe_id=risotto.id)  # doublon permis : plusieurs envies par semaine

    week_data = client.get(BASKET_URL).data
    assert week_data['week_start'] == week.isoformat() and week_data['is_current_week']
    assert [i['id'] for i in week_data['items']] == [a['id'], b['id'], c['id']]
    assert week_data['counts']['todo'] == 3 and a['source'] == 'manual' and a['servings'] == 4
    assert MealPlan.objects.filter(user=user).count() == 1  # aucune envie n'est un repas

    # Plat libre sur un créneau occupé → nouveau créneau « other » (unicité (user, date, slot_key) respectée).
    resp = client.post(f'{BASKET_URL}{b["id"]}/schedule/', {'date': tuesday.isoformat(), 'meal_time': 'dinner'})
    assert resp.status_code == 200, resp.data
    assert resp.data['status'] == 'planned' and resp.data['meal_plan']['meal_time'] == 'other'
    assert resp.data['meal_plan']['custom_label'] == 'Quiche'

    # Recette sur un créneau libre → repas standard avec le batch de l'envie et ses portions.
    resp = client.post(f'{BASKET_URL}{a["id"]}/schedule/', {'date': tuesday.isoformat(), 'meal_time': 'lunch'})
    lunch = MealPlan.objects.get(id=resp.data['meal_plan']['id'])
    assert lunch.slot_key == 'lunch'
    (mprb,) = lunch.meal_plan_recipe_batches.all()
    assert mprb.recipe_batch_id == BasketItem.objects.get(id=a['id']).recipe_batch_id and mprb.portions == 4

    # 2e risotto sur le même créneau : rejoint le repas, pas de doublon de recette.
    resp = client.post(f'{BASKET_URL}{c["id"]}/schedule/', {'date': tuesday.isoformat(), 'meal_time': 'lunch'})
    assert resp.data['meal_plan']['id'] == lunch.id and lunch.meal_plan_recipe_batches.count() == 1

    # Dé-dater : retour au panier, le créneau créé pour l'envie disparaît.
    other_id = BasketItem.objects.get(id=b['id']).meal_plan_id
    resp = client.delete(f'{BASKET_URL}{b["id"]}/schedule/')
    assert resp.data['status'] == 'todo' and resp.data['meal_plan'] is None
    assert not MealPlan.objects.filter(id=other_id).exists()

    # Les endpoints de planning existants ne voient que des repas datés.
    listed = client.get('/api/meal-plans/', {'start_date': week.isoformat(), 'end_date': tuesday.isoformat()})
    assert listed.status_code == 200


def test_cooked_from_basket_is_the_canonical_cooked(api, user, make_recipe):
    client = api.as_user(user)
    risotto = make_recipe(user, 'Risotto', ingredients=['riz', 'oignon'])
    item = _add(client, recipe_id=risotto.id)
    _add(client, label='Quiche')
    sl = client.post(f'{BASKET_URL}shopping-list/', {'item_ids': [item['id']]}, format='json').data['shopping_list']
    batch_id = BasketItem.objects.get(id=item['id']).recipe_batch_id

    preview = client.post('/api/posts/cooked/preview/', {'basket_item_id': item['id']}, format='json')
    assert preview.status_code == 200 and preview.data['display_title'] == 'Risotto'
    resp = client.post('/api/posts/cooked/', {'basket_item_id': item['id']}, format='json')
    assert resp.status_code == 201, resp.data
    assert resp.data['display_title'] == 'Risotto' and resp.data['inspired_by'] is None

    cooked = list(cooked_meal_plans(user))
    assert len(cooked) == 1
    assert list(cooked[0].meal_plan_recipe_batches.values_list('recipe_batch_id', flat=True)) == [batch_id]
    assert client.get('/api/stats/me/', {'period': 'week'}).data['cooked_count'] == 1

    # « Au feeling » : un plat libre logué sans basket_item_id coche l'envie qui correspond.
    assert client.post('/api/posts/cooked/', {'label': 'quiche'}, format='json').status_code == 201
    assert _statuses(client) == {'Risotto': 'cooked', 'Quiche': 'cooked'}
    week_items = client.get(BASKET_URL).data['items']
    assert week_items[0]['cooked_post_id'] == resp.data['id']

    # Annuler : l'envie redevient « à faire », son batch (et la liste de courses) restent.
    assert client.delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204
    assert _statuses(client)['Risotto'] == 'todo'
    assert RecipeBatch.objects.filter(id=batch_id).exists()
    assert ShoppingListBatch.objects.filter(recipe_batch_id=batch_id, shopping_list_id=sl['id']).exists()
    assert len(list(cooked_meal_plans(user))) == 1  # reste la quiche


def test_shopping_list_from_basket_merges_and_is_idempotent(api, user, make_recipe, make_shopping_list):
    client = api.as_user(user)
    risotto = make_recipe(user, 'Risotto', ingredients=['riz', 'oignon'], servings=2)
    quiche = make_recipe(user, 'Quiche', ingredients=['oeufs', 'oignon'], servings=2)
    a = _add(client, recipe_id=risotto.id, servings=4)  # × 2
    b = _add(client, recipe_id=quiche.id)
    free = _add(client, label='Salade du marché')
    existing = make_shopping_list(user, name='Maison')
    body = {'shopping_list_id': existing.id}

    first = client.post(f'{BASKET_URL}shopping-list/', body, format='json')  # défaut : envies ouvertes de la semaine
    assert first.status_code == 200, first.data
    assert sorted(first.data['added_item_ids']) == sorted([a['id'], b['id']])
    assert free['id'] not in first.data['added_item_ids']

    def totals():
        return {
            item.ingredient.name: sum(Decimal(str(q.quantity)) for q in item.quantities.all())
            for item in ShoppingListItem.objects.filter(shopping_list=existing)
        }

    # Une ligne par ingrédient, quantités par batch additionnées. Quiche sans `servings` : suit ses
    # convives (« Pour 1 : toi », foyer vide) → ½ recette pour 2 (oignon : 2 risotto + 0,5 quiche).
    assert totals() == {'riz': Decimal('2'), 'oignon': Decimal('2.5'), 'oeufs': Decimal('0.5')}

    again = client.post(
        f'{BASKET_URL}shopping-list/', {**body, 'item_ids': [a['id'], b['id'], free['id']]}, format='json'
    )
    assert again.data['added_item_ids'] == []
    assert {s['reason'] for s in again.data['skipped']} == {'already_in_list', 'no_recipe'}
    assert totals() == {'riz': Decimal('2'), 'oignon': Decimal('2.5'), 'oeufs': Decimal('0.5')}

    # Nouvelle liste : la recette passe d'une liste à l'autre (un batch = une liste), rien en double.
    new = client.post(f'{BASKET_URL}shopping-list/', {'item_ids': [b['id']], 'name': 'Week-end'}, format='json')
    assert new.status_code == 201 and new.data['created_list'] and new.data['shopping_list']['name'] == 'Week-end'
    assert totals() == {'riz': Decimal('2'), 'oignon': Decimal('2')}

    # Retirer l'envie retire ses ingrédients de la liste.
    assert client.delete(f'{BASKET_URL}{a["id"]}/').status_code == 204
    assert totals() == {}


def test_past_weeks_stay_as_history_and_carry_over_on_demand(api, user, make_recipe):
    client = api.as_user(user)
    week = current_week_start(user)
    previous = week - timedelta(days=7)
    a = _add(client, label='Gratin', week=previous.isoformat())
    b = _add(client, label='Curry', week=(previous + timedelta(days=3)).isoformat())  # n'importe quel jour
    c = _add(client, label='Tarte', week=previous.isoformat())
    cooked_at = f'{(previous + timedelta(days=2)).isoformat()}T20:00:00'
    assert (
        client.post(
            '/api/posts/cooked/', {'basket_item_id': b['id'], 'cooked_at': cooked_at}, format='json'
        ).status_code
        == 201
    )

    past = client.get(BASKET_URL, {'week': previous.isoformat()}).data
    assert not past['is_current_week'] and past['previous_week'] is None
    assert _statuses(client, week=previous.isoformat()) == {
        'Gratin': 'not_done',
        'Curry': 'cooked',
        'Tarte': 'not_done',
    }
    assert client.get(BASKET_URL).data['previous_week'] == {'week_start': previous.isoformat(), 'open_count': 2}

    first = client.post(f'{BASKET_URL}carry-over/', {'item_ids': [a['id']]}, format='json')
    assert first.status_code == 201 and first.data['carried_over_count'] == 1
    assert [i['display_title'] for i in first.data['week']['items']] == ['Gratin']
    rest = client.post(f'{BASKET_URL}carry-over/', {}, format='json')
    assert rest.data['carried_over_count'] == 1  # seule la tarte : déjà reporté / cuisiné exclus
    assert client.post(f'{BASKET_URL}carry-over/', {}, format='json').data['carried_over_count'] == 0

    assert _statuses(client, week=previous.isoformat()) == {
        'Gratin': 'carried_over',
        'Curry': 'cooked',
        'Tarte': 'carried_over',
    }
    assert _statuses(client) == {'Gratin': 'todo', 'Tarte': 'todo'}
    assert BasketItem.objects.filter(user=user).count() == 5  # rien n'est supprimé
    assert c['id'] in BasketItem.objects.filter(carried_over_to__isnull=False).values_list('id', flat=True)
