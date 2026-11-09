"""Conflits alimentaires (POST /api/dietary-conflicts/) : permissions + règles de matching."""

import pytest

pytestmark = pytest.mark.django_db

URL = '/api/dietary-conflicts/'


@pytest.fixture
def host_and_guest(user, make_user, follow):
    guest = make_user('guest')
    follow(user, guest)  # relation complice (un sens suffit)
    return user, guest


def _conflicts(api, host, guest, recipes):
    resp = api.as_user(host).post(
        URL, {'target_user_id': guest.id, 'recipe_ids': [r.id for r in recipes]}, format='json'
    )
    assert resp.status_code == 200, resp.data
    return {c['recipe_id']: c['reasons'] for c in resp.data['conflicts']}


def test_requires_target_and_complice_relationship(api, user, make_user):
    client = api.as_user(user)
    assert client.post(URL, {'recipe_ids': []}, format='json').status_code == 400
    assert client.post(URL, {'target_user_id': 999999}, format='json').status_code == 404
    assert client.post(URL, {'target_user_id': make_user().id, 'recipe_ids': []}, format='json').status_code == 403


def test_allergy_and_dislike_conflicts_are_reported(api, host_and_guest, make_recipe):
    host, guest = host_and_guest
    guest.allergies = ['Lactose / lait']
    guest.food_dislikes = ['Olives']
    guest.save(update_fields=['allergies', 'food_dislikes'])

    gratin = make_recipe(host, 'Gratin', ingredients=['crème fraîche', 'pommes de terre'])
    tapenade = make_recipe(host, 'Tapenade', ingredients=['olives noires', 'câpres'])
    neutre = make_recipe(host, 'Riz', ingredients=['riz', 'tomate'])

    conflicts = _conflicts(api, host, guest, [gratin, tapenade, neutre])

    assert conflicts == {gratin.id: ['allergy'], tapenade.id: ['dislike']}


@pytest.mark.xfail(
    strict=True,
    reason='ITEM-20261109-1100-bg01 : « Olives » (dislike) matche « huile d’olive » par sous-chaîne',
)
def test_olive_oil_is_not_a_conflict_for_olive_dislike(api, host_and_guest, make_recipe):
    host, guest = host_and_guest
    guest.food_dislikes = ['Olives']
    guest.save(update_fields=['food_dislikes'])
    vinaigrette = make_recipe(host, 'Vinaigrette', ingredients=["huile d'olive", 'vinaigre'])

    assert _conflicts(api, host, guest, [vinaigrette]) == {}


@pytest.mark.xfail(
    strict=True,
    reason=(
        'ITEM-20261109-1100-bg01 : même cause (sous-chaîne) — « lait » matche « laitue » dans '
        'conflicting_recipe_ids_for_user, la recette remonte en conflit avec reasons=[]'
    ),
)
def test_lettuce_is_not_a_conflict_for_milk_allergy(api, host_and_guest, make_recipe):
    host, guest = host_and_guest
    guest.allergies = ['Lactose / lait']
    guest.save(update_fields=['allergies'])
    salade = make_recipe(host, 'Salade', ingredients=['laitue', 'tomate'])

    assert _conflicts(api, host, guest, [salade]) == {}
