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


def test_olive_oil_is_not_a_conflict_for_olive_dislike(api, host_and_guest, make_recipe):
    host, guest = host_and_guest
    guest.food_dislikes = ['Olives']
    guest.save(update_fields=['food_dislikes'])
    vinaigrette = make_recipe(host, 'Vinaigrette', ingredients=["huile d'olive", 'vinaigre'])

    assert _conflicts(api, host, guest, [vinaigrette]) == {}


def test_lettuce_is_not_a_conflict_for_milk_allergy(api, host_and_guest, make_recipe):
    host, guest = host_and_guest
    guest.allergies = ['Lactose / lait']
    guest.save(update_fields=['allergies'])
    salade = make_recipe(host, 'Salade', ingredients=['laitue', 'tomate'])

    assert _conflicts(api, host, guest, [salade]) == {}


def test_compound_rules_milk_and_peanut(api, host_and_guest, make_recipe):
    """« lait de coco » n'est pas du lait ; « beurre de cacahuète » reste un conflit arachide."""
    host, guest = host_and_guest
    guest.allergies = ['Lactose / lait']
    guest.save(update_fields=['allergies'])
    curry = make_recipe(host, 'Curry', ingredients=['lait de coco', 'riz'])
    satay = make_recipe(host, 'Satay', ingredients=['beurre de cacahuète'])
    assert _conflicts(api, host, guest, [curry, satay]) == {}

    guest.allergies = ['Arachides']
    guest.save(update_fields=['allergies'])
    assert _conflicts(api, host, guest, [satay]) == {satay.id: ['allergy']}


DISMISS = '/api/dietary-conflicts/dismiss/'
EXCEPTIONS = '/api/dietary-conflicts/exceptions/'


def _hit(api, host, guest, recipe):
    resp = api.as_user(host).post(URL, {'target_user_id': guest.id, 'recipe_ids': [recipe.id]}, format='json')
    return resp.data['conflicts'][0]['hits'][0]


def test_dismissed_dislike_is_scoped_to_the_host(api, host_and_guest, make_user, follow, make_recipe):
    host, guest = host_and_guest
    other_host = make_user('other_host')
    follow(other_host, guest)
    guest.food_dislikes = ['Olives']
    guest.save(update_fields=['food_dislikes'])
    tapenade = make_recipe(host, 'Tapenade', ingredients=['olives noires'])

    hit = _hit(api, host, guest, tapenade)
    assert hit['reason'] == 'dislike' and hit['label'] == 'Olives' and hit['ingredient'] == 'olives noires'
    body = {'target_user_id': guest.id, 'kind': 'dislike', 'label': hit['label'], 'ingredient_id': hit['ingredient_id']}
    resp = api.as_user(host).post(DISMISS, body, format='json')
    assert resp.status_code == 201, resp.data
    assert resp.data['applies_to'] == 'creator'
    assert api.as_user(host).post(DISMISS, body, format='json').status_code == 200  # idempotent

    assert _conflicts(api, host, guest, [tapenade]) == {}
    assert _conflicts(api, other_host, guest, [tapenade]) == {tapenade.id: ['dislike']}

    # L'invité voit l'exception posée sur ses préférences et peut la retirer.
    listed = api.as_user(guest).get(EXCEPTIONS).data['exceptions']
    assert [e['id'] for e in listed] == [resp.data['id']]
    assert api.as_user(other_host).delete(f'{EXCEPTIONS}{resp.data["id"]}/').status_code == 404
    assert api.as_user(guest).delete(f'{EXCEPTIONS}{resp.data["id"]}/').status_code == 204
    assert _conflicts(api, host, guest, [tapenade]) == {tapenade.id: ['dislike']}


def test_dismiss_permissions(api, host_and_guest, make_user, make_recipe):
    host, guest = host_and_guest
    guest.allergies = ['Lactose / lait']
    guest.food_dislikes = ['Olives']
    guest.save(update_fields=['allergies', 'food_dislikes'])
    gratin = make_recipe(host, 'Gratin', ingredients=['crème fraîche'])
    ing_id = _hit(api, host, guest, gratin)['ingredient_id']
    allergy = {'target_user_id': guest.id, 'kind': 'allergy', 'label': 'Lactose / lait', 'ingredient_id': ing_id}

    # Un tiers non complice : refusé ; l'hôte ne peut pas lever une allergie.
    assert api.as_user(make_user('stranger')).post(DISMISS, allergy, format='json').status_code == 403
    assert api.as_user(host).post(DISMISS, allergy, format='json').status_code == 403
    # Préférence non déclarée / type inconnu.
    bad_label = {**allergy, 'kind': 'dislike', 'label': 'Brocolis'}
    assert api.as_user(host).post(DISMISS, bad_label, format='json').status_code == 400
    assert api.as_user(guest).post(DISMISS, {**allergy, 'kind': 'nope'}, format='json').status_code == 400

    # La personne elle-même peut lever sa propre alerte allergie : vaut pour tous.
    resp = api.as_user(guest).post(DISMISS, {**allergy, 'target_user_id': None}, format='json')
    assert resp.status_code == 201, resp.data
    assert resp.data['applies_to'] == 'all'
    assert _conflicts(api, host, guest, [gratin]) == {}
