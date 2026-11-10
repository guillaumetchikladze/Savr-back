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


def test_basket_create_idempotent_with_client_request_id(api, user, make_recipe):
    client = api.as_user(user)
    risotto = make_recipe(user, 'Risotto')
    week = current_week_start(user)
    tuesday = week + timedelta(days=1)

    # 1. 1er appel avec client_request_id : 201 créé
    payload = {
        'recipe_id': risotto.id,
        'date': tuesday.isoformat(),
        'meal_time': 'dinner',
        'client_request_id': 'req-week-proposal-1',
    }
    resp1 = client.post(BASKET_URL, payload, format='json')
    assert resp1.status_code == 201, resp1.data
    item_id = resp1.data['id']
    assert BasketItem.objects.filter(user=user).count() == 1

    # 2. Rejeu immédiat avec le même client_request_id : 200 renvoyé, aucun doublon
    resp2 = client.post(BASKET_URL, payload, format='json')
    assert resp2.status_code == 200, resp2.data
    assert resp2.data['id'] == item_id
    assert resp2.headers.get('Idempotent-Replayed') == 'true'
    assert BasketItem.objects.filter(user=user).count() == 1

    # 3. Clé trop longue (> 64) : 400
    long_payload = {**payload, 'client_request_id': 'x' * 65}
    resp_long = client.post(BASKET_URL, long_payload, format='json')
    assert resp_long.status_code == 400
    assert resp_long.data['code'] == 'invalid_client_request_id'


def test_basket_create_deduplicates_uncooked_slot(api, user, make_recipe):
    client = api.as_user(user)
    tarte = make_recipe(user, 'Tarte aux poireaux')
    week = current_week_start(user)
    thursday = week + timedelta(days=3)

    # 1. Ajout daté sans client_request_id
    data = {
        'recipe_id': tarte.id,
        'date': thursday.isoformat(),
        'meal_time': 'dinner',
    }
    resp1 = client.post(BASKET_URL, data, format='json')
    assert resp1.status_code == 201
    item_id = resp1.data['id']

    # 2. Retap avec même recette, même date et même créneau : renvoie l'existant en 200
    resp2 = client.post(BASKET_URL, data, format='json')
    assert resp2.status_code == 200
    assert resp2.data['id'] == item_id
    assert BasketItem.objects.filter(user=user).count() == 1

    # 3. Plat libre dédoublonné également
    free_dish = {
        'label': 'Soupe maison',
        'date': thursday.isoformat(),
        'meal_time': 'lunch',
    }
    resp_free1 = client.post(BASKET_URL, free_dish, format='json')
    assert resp_free1.status_code == 201
    free_id = resp_free1.data['id']

    resp_free2 = client.post(BASKET_URL, free_dish, format='json')
    assert resp_free2.status_code == 200
    assert resp_free2.data['id'] == free_id
    assert BasketItem.objects.filter(user=user).count() == 2


def _dated(recipe, day, request_id, **extra):
    return {
        'recipe_id': recipe.id,
        'date': day.isoformat(),
        'meal_time': 'dinner',
        'client_request_id': request_id,
        **extra,
    }


def test_client_request_id_replay_after_delete_recreates(api, user, make_recipe):
    """Caler → Annuler → Caler (même id) : l'envie supprimée n'est pas « rejouée », elle est recréée."""
    client = api.as_user(user)
    risotto = make_recipe(user, 'Risotto')
    tuesday = current_week_start(user) + timedelta(days=1)
    payload = _dated(risotto, tuesday, 'week:card:1')

    first = client.post(BASKET_URL, payload, format='json')
    assert first.status_code == 201, first.data
    assert client.delete(f'{BASKET_URL}{first.data["id"]}/').status_code == 204
    assert not BasketItem.objects.filter(user=user).exists()

    again = client.post(BASKET_URL, payload, format='json')
    assert again.status_code == 201, again.data
    assert again.data['id'] != first.data['id']
    assert BasketItem.objects.filter(user=user, id=again.data['id']).exists()

    # Et le nouvel élément est bien celui rejoué ensuite.
    replay = client.post(BASKET_URL, payload, format='json')
    assert replay.status_code == 200
    assert replay.data['id'] == again.data['id']
    assert replay.headers.get('Idempotent-Replayed') == 'true'
    assert BasketItem.objects.filter(user=user).count() == 1


def test_client_request_id_with_other_payload_is_a_conflict(api, user, make_recipe):
    client = api.as_user(user)
    risotto, gratin = make_recipe(user, 'Risotto'), make_recipe(user, 'Gratin')
    tuesday = current_week_start(user) + timedelta(days=1)

    assert client.post(BASKET_URL, _dated(risotto, tuesday, 'same-id'), format='json').status_code == 201
    conflict = client.post(BASKET_URL, _dated(gratin, tuesday, 'same-id'), format='json')
    assert conflict.status_code == 409
    assert conflict.data['error'] == 'idempotency_key_reused'
    assert list(BasketItem.objects.filter(user=user).values_list('recipe_id', flat=True)) == [risotto.id]


def test_client_request_id_is_scoped_per_user(make_user, make_recipe):
    from rest_framework.test import APIClient

    alice, bob = make_user('alice'), make_user('bob')
    created = []
    for who in (alice, bob):
        client = APIClient()
        client.force_authenticate(user=who)
        recipe = make_recipe(who, 'Risotto')
        tuesday = current_week_start(who) + timedelta(days=1)
        resp = client.post(BASKET_URL, _dated(recipe, tuesday, 'shared-id'), format='json')
        assert resp.status_code == 201, resp.data
        created.append(resp.data['id'])
    assert BasketItem.objects.get(id=created[0]).user_id == alice.id
    assert BasketItem.objects.get(id=created[1]).user_id == bob.id


def test_client_request_id_is_claimed_before_creation_and_released_on_error(api, user, make_recipe):
    """La clé est réservée (contrainte unique) avant la création, dans la même transaction : une requête
    concurrente attend puis rejoue. Une erreur métier libère la clé."""
    from unittest import mock

    from core.models import IdempotencyRecord
    from recipes.services import basket_service

    client = api.as_user(user)
    risotto = make_recipe(user, 'Risotto')
    tuesday = current_week_start(user) + timedelta(days=1)
    seen = []
    real_add_item = basket_service.add_item

    def spy(*args, **kwargs):
        seen.append(list(IdempotencyRecord.objects.filter(user=user).values_list('key', 'status_code')))
        return real_add_item(*args, **kwargs)

    with mock.patch.object(basket_service, 'add_item', side_effect=spy):
        assert client.post(BASKET_URL, _dated(risotto, tuesday, 'claim-1'), format='json').status_code == 201
    assert seen == [[('basket:claim-1', None)]]
    record = IdempotencyRecord.objects.get(user=user, key='basket:claim-1')
    assert record.status_code == 201

    bad = client.post(BASKET_URL, {'recipe_id': 999999, 'client_request_id': 'claim-2'}, format='json')
    assert bad.status_code >= 400
    assert not IdempotencyRecord.objects.filter(user=user, key='basket:claim-2').exists()


def test_slot_dedup_ignores_servings_and_excludes_cooked(api, user, make_recipe):
    client = api.as_user(user)
    tarte = make_recipe(user, 'Tarte')
    thursday = current_week_start(user) + timedelta(days=3)
    base = {'recipe_id': tarte.id, 'date': thursday.isoformat(), 'meal_time': 'dinner'}

    first = client.post(BASKET_URL, {**base, 'servings': 2}, format='json')
    assert first.status_code == 201
    same_slot = client.post(BASKET_URL, {**base, 'servings': 6}, format='json')
    assert same_slot.status_code == 200
    assert same_slot.data['id'] == first.data['id']

    MealPlan.objects.filter(id=first.data['meal_plan']['id']).update(confirmed=True)
    after_cooked = client.post(BASKET_URL, base, format='json')
    assert after_cooked.status_code == 201
    assert after_cooked.data['id'] != first.data['id']
