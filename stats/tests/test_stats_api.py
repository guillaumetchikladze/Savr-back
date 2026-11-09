"""Endpoints stats : permissions (profil visible), période."""

from datetime import date
from unittest import mock

import pytest

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 9)


@pytest.fixture(autouse=True)
def _frozen_today():
    with mock.patch('django.utils.timezone.localdate', return_value=TODAY):
        yield


def _cook_n(owner, make_meal_plan, make_recipe, n, **recipe_extra):
    recipe = make_recipe(owner, 'Curry', ingredients=['pois chiches', 'tomate'], **recipe_extra)
    for i in range(n):
        make_meal_plan(owner, day=date(2026, 3 + i, 10), recipes=[recipe], confirmed=True)


def test_stats_me_shape_and_period_validation(api, user, make_meal_plan, make_recipe):
    _cook_n(user, make_meal_plan, make_recipe, 2, cuisine='indienne')
    client = api.as_user(user)

    resp = client.get('/api/stats/me/', {'period': 'year', 'year': 2026})
    assert resp.status_code == 200
    assert resp.data['period'] == {'type': 'year', 'start': '2026-01-01', 'end': '2026-12-31', 'year': 2026}
    assert resp.data['cooked_count'] == 2
    assert resp.data['cuisines'][0] == {
        'code': 'indienne',
        'label': 'Indienne',
        'flag': '🇮🇳',
        'country': 'IN',
        'count': 2,
    }
    assert client.get('/api/stats/me/', {'period': 'decade'}).status_code == 400
    assert client.get('/api/stats/me/', {'period': 'year', 'year': 2031}).status_code == 400


def test_other_user_stats_require_visible_profile(api, user, make_user, make_meal_plan, make_recipe, follow):
    bob = make_user('bob')
    _cook_n(bob, make_meal_plan, make_recipe, 1)
    client = api.as_user(user)

    assert client.get(f'/api/stats/users/{bob.id}/').status_code == 403

    follow(user, bob)
    resp = client.get(f'/api/stats/users/{bob.id}/', {'period': 'all'})
    assert resp.status_code == 200
    assert resp.data['cooked_count'] == 1 and resp.data['badges_count'] == 0
    assert 'hosted' not in resp.data and 'top_miam_post' not in resp.data  # sous-ensemble public
    assert client.get('/api/stats/users/999999/').status_code == 404
