"""« Je l'ai déjà » : repas planifiés passés à confirmer (créneau passé, « Pas fait » exclu, ≤ 3, Oui = cuisiné)."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from recipes.models import BasketItem, CookedConfirmationDismissal, MealPlan
from recipes.services.basket_service import monday_of, user_today
from recipes.services.pending_confirmations import pending_confirmations

PENDING = '/api/cooked/pending-confirmations/'
PARIS = ZoneInfo('Europe/Paris')


def _at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=PARIS)


def test_only_meals_whose_slot_has_passed(user, make_recipe, make_meal_plan):
    today = user_today(user)
    quiche = make_recipe(user, 'Quiche')
    lunch = make_meal_plan(user, day=today, meal_time='lunch', recipes=[quiche])
    dinner = make_meal_plan(user, day=today, meal_time='dinner', custom_label='Pâtes au pesto', meal_type='recipe')
    early = make_meal_plan(
        user, day=today, meal_time='other', slot_key='gouter', scheduled_time=time(16, 0), recipes=[quiche]
    )
    make_meal_plan(user, day=today, meal_time='breakfast', meal_type='cantine', custom_label='Cantine')
    make_meal_plan(user, day=today - timedelta(days=2), meal_time='dinner', recipes=[quiche])  # trop ancien
    make_meal_plan(user, day=today - timedelta(days=1), meal_time='lunch', recipes=[quiche], confirmed=True)

    assert [i.meal_plan.id for i in pending_confirmations(user, now=_at(today, 13))] == []
    assert [i.meal_plan.id for i in pending_confirmations(user, now=_at(today, 14))] == [lunch.id]
    # Heure prévue 16:00 + 1 h de marge.
    assert [i.meal_plan.id for i in pending_confirmations(user, now=_at(today, 17))] == [early.id, lunch.id]
    items = pending_confirmations(user, now=_at(today, 21, 30))
    assert [i.meal_plan.id for i in items] == [dinner.id, early.id, lunch.id]  # le plus récent d'abord
    assert items[0].payload()['question'] == 'Tu as cuisiné Pâtes au pesto ?'
    assert items[0].cooked_payload == {'meal_plan_id': dinner.id}


def test_api_caps_at_three_and_dismissal_is_remembered(api, user, make_user, make_recipe, make_meal_plan):
    yesterday = user_today(user) - timedelta(days=1)  # tous les créneaux d'hier sont passés
    dal = make_recipe(user, 'Dal')
    meals = [make_meal_plan(user, day=yesterday, meal_time=slot, recipes=[dal]) for slot in ('breakfast', 'lunch')]
    dinner = make_meal_plan(user, day=yesterday, meal_time='dinner', custom_label='Risotto', meal_type='recipe')
    item = BasketItem.objects.create(user=user, week_start=monday_of(yesterday), label='Risotto', meal_plan=dinner)
    meals.append(
        make_meal_plan(user, day=yesterday, meal_time='other', slot_key='x', scheduled_time=time(16), recipes=[dal])
    )
    client = api.as_user(user)

    body = client.get(PENDING).json()
    assert body['count'] == 3
    first = body['items'][0]
    assert (first['kind'], first['id'], first['meal_plan_id']) == ('basket_item', item.id, dinner.id)
    assert first['cooked_payload'] == {'basket_item_id': item.id}

    res = client.post(first['dismiss_url'])
    assert res.status_code == 200 and res.json()['dismissed'] is True
    assert dinner.id not in [i['meal_plan_id'] for i in res.json()['items']]
    item.refresh_from_db()
    assert item.meal_plan_id == dinner.id  # « Pas fait » : l'envie reste dans le panier, datée
    assert client.post(first['dismiss_url']).status_code == 200  # idempotent
    assert CookedConfirmationDismissal.objects.filter(user=user).count() == 1
    assert dinner.id not in [i['meal_plan_id'] for i in client.get(PENDING).json()['items']]

    # Annuler le « Pas fait » ; on ne peut pas refuser le repas d'un autre.
    assert client.delete(first['dismiss_url']).json()['items'][0]['meal_plan_id'] == dinner.id
    other = make_meal_plan(make_user('bob'), day=yesterday, meal_time='dinner', recipes=[dal])
    assert client.post(f'{PENDING}meal_plan/{other.id}/dismiss/').status_code == 404


def test_yes_logs_the_meal_and_removes_it(api, user, make_recipe, make_meal_plan):
    yesterday = user_today(user) - timedelta(days=1)
    meal = make_meal_plan(user, day=yesterday, meal_time='dinner', recipes=[make_recipe(user, 'Pâtes au pesto')])
    client = api.as_user(user)
    (pending,) = client.get(PENDING).json()['items']

    res = client.post('/api/posts/cooked/', pending['cooked_payload'], format='json')
    assert res.status_code == 201
    meal.refresh_from_db()
    assert meal.confirmed and MealPlan.objects.filter(user=user).count() == 1
    assert client.get(PENDING).json() == {'items': [], 'count': 0}
