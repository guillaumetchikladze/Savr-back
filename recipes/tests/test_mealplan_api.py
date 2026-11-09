"""Planning : création de créneau, ajout de recettes, fenêtre timeline, invitations repas."""

from datetime import date, timedelta

import pytest

from accounts.models import Notification
from recipes.models import MealInvitation, MealPlan

pytestmark = pytest.mark.django_db

DAY = date(2026, 3, 10)


def _window(api, start=DAY - timedelta(days=3), end=DAY + timedelta(days=3)):
    return api.get(
        '/api/meal-plans/timeline-window/',
        {'date__gte': start.isoformat(), 'date__lte': end.isoformat()},
    )


def test_create_slot_with_recipes_then_add_more(api, user, make_recipe):
    r1, r2 = make_recipe(user), make_recipe(user)
    client = api.as_user(user)

    resp = client.post(
        '/api/meal-plans/',
        {'date': DAY.isoformat(), 'meal_time': 'dinner', 'meal_type': 'recipe', 'recipe_ids': [r1.id]},
        format='json',
    )
    assert resp.status_code == 201, resp.data
    mp_id = resp.data['id']
    assert MealPlan.objects.get(id=mp_id).user == user

    added = client.post(f'/api/meal-plans/{mp_id}/add-recipes/', {'recipe_ids': [r1.id, r2.id]}, format='json')
    assert added.status_code == 200
    assert added.data['added_recipe_ids'] == [r2.id]
    assert added.data['already_present_recipe_ids'] == [r1.id]

    assert client.post(f'/api/meal-plans/{mp_id}/add-recipes/', {'recipe_ids': []}, format='json').status_code == 400


def test_stranger_cannot_touch_someone_else_meal_plan(api, user, make_user, make_recipe, make_meal_plan):
    mp = make_meal_plan(user, day=DAY)
    stranger = make_user()
    client = api.as_user(stranger)

    assert client.get(f'/api/meal-plans/{mp.id}/').status_code == 404
    resp = client.post(
        f'/api/meal-plans/{mp.id}/add-recipes/', {'recipe_ids': [make_recipe(stranger).id]}, format='json'
    )
    assert resp.status_code == 404


def test_timeline_window_scopes_to_owned_and_invited_meals(api, user, make_user, make_meal_plan):
    host = make_user('host')
    own = make_meal_plan(user, day=DAY)
    out_of_window = make_meal_plan(user, day=DAY + timedelta(days=30))
    accepted = make_meal_plan(host, day=DAY, meal_time='dinner')
    pending = make_meal_plan(host, day=DAY + timedelta(days=1))
    declined = make_meal_plan(host, day=DAY + timedelta(days=2))
    foreign = make_meal_plan(make_user(), day=DAY)
    for mp, status in ((accepted, 'accepted'), (pending, 'pending'), (declined, 'declined')):
        MealInvitation.objects.create(inviter=host, invitee=user, meal_plan=mp, status=status)

    client = api.as_user(user)
    assert client.get('/api/meal-plans/timeline-window/').status_code == 400

    resp = _window(client)
    assert resp.status_code == 200
    ids = {m['id'] for m in resp.data['meal_plans']}
    assert ids == {own.id, accepted.id, pending.id}
    assert out_of_window.id not in ids and declined.id not in ids and foreign.id not in ids
    assert [
        i['meal_plan']['id'] if isinstance(i['meal_plan'], dict) else i['meal_plan'] for i in resp.data['invitations']
    ] == [pending.id]


def test_invitation_accept_and_decline(api, user, make_user, make_meal_plan):
    host = make_user('host')
    mp_a = make_meal_plan(host, day=DAY)
    mp_b = make_meal_plan(host, day=DAY, meal_time='dinner')
    inv_a = MealInvitation.objects.create(inviter=host, invitee=user, meal_plan=mp_a)
    inv_b = MealInvitation.objects.create(inviter=host, invitee=user, meal_plan=mp_b)

    # L'hôte ne peut pas accepter à la place de l'invité (scope invitee par défaut).
    assert api.as_user(host).post(f'/api/meal-invitations/{inv_a.id}/accept/').status_code == 404

    client = api.as_user(user)
    assert client.post(f'/api/meal-invitations/{inv_a.id}/accept/').status_code == 200
    assert client.post(f'/api/meal-invitations/{inv_a.id}/accept/').status_code == 400  # déjà traitée
    assert client.post(f'/api/meal-invitations/{inv_b.id}/decline/').status_code == 200

    inv_a.refresh_from_db()
    inv_b.refresh_from_db()
    assert (inv_a.status, inv_b.status) == ('accepted', 'declined')
    assert Notification.objects.filter(user=host, notification_type='meal_invitation').count() == 1

    ids = {m['id'] for m in _window(client).data['meal_plans']}
    assert mp_a.id in ids and mp_b.id not in ids
