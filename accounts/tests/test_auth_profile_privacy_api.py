"""Profil d'un autre (`GET /api/auth/users/<id>/`, listes d'abonnés) : ni email, ni allergies hors amis mutuels."""

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def victim(make_user):
    u = make_user('victim')
    u.allergies, u.food_dislikes, u.regimes = ['arachide'], ['coriandre'], ['vegan']
    u.save(update_fields=['allergies', 'food_dislikes', 'regimes'])
    return u


def test_stranger_gets_neither_email_nor_dietary_data(api, make_user, victim):
    data = api.as_user(make_user('stranger')).get(f'/api/auth/users/{victim.id}/').data
    assert data['username'] == 'victim' and 'email' not in data
    assert data['allergies'] == [] and data['food_dislikes'] == [] and data['regimes'] == []


def test_mutual_friend_sees_dietary_data_but_not_email(api, make_user, follow, victim):
    friend = make_user('friend')
    follow(friend, victim)
    follow(victim, friend)
    client = api.as_user(friend)
    data = client.get(f'/api/auth/users/{victim.id}/').data
    assert 'email' not in data and data['allergies'] == ['arachide'] and data['regimes'] == ['vegan']
    listed = client.get(f'/api/auth/users/{friend.id}/followers/').data
    rows = listed['results'] if isinstance(listed, dict) else listed
    assert rows and all('email' not in r for r in rows)


def test_own_profile_keeps_everything(api, victim):
    data = api.as_user(victim).get('/api/auth/profile/').data
    assert data['email'] == victim.email and data['allergies'] == ['arachide']
