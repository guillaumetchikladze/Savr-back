"""Endpoints stats / Récap : permissions (profil visible), période, disponibilité et historique mince du Récap."""

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


def test_recap_not_available_before_december(api, user, settings):
    settings.RECAP_AVAILABLE_FROM = '12-01'
    resp = api.as_user(user).get('/api/recap/2026/')
    assert resp.status_code == 200
    assert resp.data == {'year': 2026, 'available': False, 'available_from': '2026-12-01', 'slides': []}
    assert api.as_user(user).get('/api/recap/2027/').status_code == 400


def test_recap_thin_history(api, user, settings, make_meal_plan, make_recipe):
    settings.RECAP_FORCE_AVAILABLE = True
    _cook_n(user, make_meal_plan, make_recipe, 2)

    data = api.as_user(user).get('/api/recap/2026/').data

    assert data['thin'] is True and data['persona'] is None
    assert [s['id'] for s in data['slides']] == ['starting_now']
    assert data['slides'][0]['data'] == {'min_cooked': 3, 'missing': 1, 'next_year': 2027}


def test_recap_full_slides_for_staff_preview(api, make_user, make_meal_plan, make_recipe):
    staff = make_user('chef', is_staff=True)
    _cook_n(staff, make_meal_plan, make_recipe, 4, cuisine='marocaine')

    data = api.as_user(staff).get('/api/recap/2026/').data

    assert data['available'] is True and data['thin'] is False
    ids = [s['id'] for s in data['slides']]
    assert ids == ['year_in_cooking', 'star_dish', 'ingredients', 'world_tour', 'persona']
    year_slide = data['slides'][0]
    assert year_slide['big_number'] == 4 and len(year_slide['data']['months']) == 12
    assert data['slides'][1]['subtitle'] == 'Curry'
    assert data['persona']['code'] == 'fidele'
    for slide in data['slides']:
        assert {'id', 'title', 'big_number', 'unit', 'subtitle', 'data'} <= set(slide)


def test_other_user_stats_expose_earned_badges_and_top_dishes(
    api, user, make_user, make_meal_plan, make_recipe, follow
):
    from accounts.models import UserBlock
    from stats.models import UserBadge

    bob = make_user('bob')
    _cook_n(bob, make_meal_plan, make_recipe, 2)
    UserBadge.objects.create(user=bob, code='first_cooked', context={'value': 1})
    UserBadge.objects.create(user=bob, code='retired_badge')  # code inconnu du registre : ignoré
    follow(user, bob)
    client = api.as_user(user)

    resp = client.get(f'/api/stats/users/{bob.id}/', {'period': 'all'})

    assert resp.status_code == 200
    assert resp.data['badges_count'] == 1
    badge = resp.data['badges'][0]
    assert badge['code'] == 'first_cooked' and badge['label'] == 'Premier plat' and badge['earned_at']
    assert 'context' not in badge
    assert resp.data['top_dishes'] == [{'label': 'Curry', 'count': 2, 'last_cooked_on': '2026-04-10'}]

    # Blocage (dans un sens ou l'autre) : profil fermé, badges compris.
    UserBlock.objects.create(blocker=bob, blocked=user)
    assert client.get(f'/api/stats/users/{bob.id}/').status_code == 403


def test_badges_me_all_returns_every_locked_badge(api, user):
    from stats.services.badges import BADGES

    client = api.as_user(user)
    assert len(client.get('/api/badges/me/').data['next']) == 5
    locked = client.get('/api/badges/me/', {'all': '1'}).data['next']
    assert len(locked) == len(BADGES)
    assert all({'current', 'target', 'ratio'} == set(b['progress']) for b in locked)


def test_recap_status_is_light_and_full_recap_is_cached(api, make_user, settings, make_meal_plan, make_recipe):
    settings.RECAP_AVAILABLE_FROM = '12-01'
    staff, regular = make_user('chef', is_staff=True), make_user('reg')
    _cook_n(staff, make_meal_plan, make_recipe, 3)

    with mock.patch('stats.services.recap.build_recap', side_effect=AssertionError('no build')):
        resp = api.as_user(regular).get('/api/recap/2026/status/')
    assert resp.data == {'year': 2026, 'available': False, 'available_from': '2026-12-01'}
    assert api.as_user(regular).get('/api/recap/2019/status/').status_code == 400

    client = api.as_user(staff)
    assert client.get('/api/recap/2026/status/').data['available'] is True
    first = client.get('/api/recap/2026/').data
    with mock.patch('stats.services.recap.build_recap', side_effect=AssertionError('cache miss')):
        assert client.get('/api/recap/2026/').data == first

    # Nouveau « cuisiné » : le cache est invalidé.
    make_meal_plan(staff, day=date(2026, 11, 2), recipes=[make_recipe(staff, 'Tajine')], confirmed=True)
    assert client.get('/api/recap/2026/').data['cooked_count'] == 4
