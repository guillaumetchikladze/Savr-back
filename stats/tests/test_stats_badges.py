"""Badges : attribution, idempotence, backfill, `new_badges` de POST /posts/cooked/, GET /badges/me/."""

from datetime import date

import pytest
from django.core.management import call_command

from recipes.models import MealInvitation
from stats.models import UserBadge
from stats.services.badges import BADGES, evaluate_badges

pytestmark = pytest.mark.django_db


# Badges historiques (les loufoques ont leurs propres tests : test_stats_badges_quirky.py).
HISTORIC = {b.code for b in BADGES if b.family in ('premiere_fois', 'ingredient', 'collection', 'serie', 'hote')}


def _codes(badges):
    return {b['code'] for b in badges} & HISTORIC


def test_registry_has_mvp_set():
    codes = [b.code for b in BADGES]
    assert len(codes) == len(set(codes)) >= 20
    assert {'first_cooked', 'first_photo', 'ingredient_aubergine', 'cuisines_5', 'streak_4', 'host_5'} <= set(codes)


def test_evaluate_awards_once_with_context(user, make_user, make_recipe, make_meal_plan):
    recipe = make_recipe(user, 'Moussaka', ingredients=['aubergine', 'sel'])
    days = [date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26)]
    meals = [
        make_meal_plan(user, day=d, recipes=[recipe], confirmed=True, guest_count=2 if i == 0 else 0)
        for i, d in enumerate(days)
    ]
    for i in range(3):
        MealInvitation.objects.create(
            inviter=user, invitee=make_user(f'guest{i}'), meal_plan=meals[1], status='accepted'
        )

    new = evaluate_badges(user)

    assert _codes(new) == {'first_cooked', 'ingredient_aubergine', 'streak_4', 'host_5'}
    by_code = {b['code']: b for b in new}
    assert by_code['first_cooked']['context']['achieved_on'] == '2026-10-05'
    assert by_code['streak_4']['context']['achieved_on'] == '2026-10-26'
    assert by_code['host_5']['context'] == {'achieved_on': '2026-10-12', 'value': 5}
    assert evaluate_badges(user) == []  # idempotent
    assert UserBadge.objects.filter(user=user, code__in=HISTORIC).count() == 4


def test_backfill_command_is_idempotent(user, make_user, make_recipe, make_meal_plan):
    other = make_user('bob')
    make_meal_plan(user, recipes=[make_recipe(user, ingredients=['saumon'])], confirmed=True)
    make_meal_plan(other, recipes=[make_recipe(other)], confirmed=False)  # planifié ≠ cuisiné

    call_command('backfill_badges')
    call_command('backfill_badges')

    assert set(UserBadge.objects.filter(user=user, code__in=HISTORIC).values_list('code', flat=True)) == {
        'first_cooked',
        'ingredient_saumon',
    }
    assert not UserBadge.objects.filter(user=other).exists()


def test_cooked_log_returns_new_badges_then_none(api, user, make_recipe):
    recipe = make_recipe(user, 'Caviar d’aubergine', ingredients=['aubergine'])
    client = api.as_user(user)

    first = client.post('/api/posts/cooked/', {'recipe_id': recipe.id, 'publish': False}, format='json')
    again = client.post('/api/posts/cooked/', {'label': 'Tartines', 'publish': False}, format='json')

    assert first.status_code == 201, first.data
    assert _codes(first.data['new_badges']) == {'first_cooked', 'ingredient_aubergine'}
    badge = next(b for b in first.data['new_badges'] if b['code'] == 'ingredient_aubergine')
    legacy = {'code', 'title', 'emoji', 'description', 'category', 'earned_at', 'context'}  # app 1.0.9
    added = {'family', 'family_label', 'phrase', 'secret', 'hint', 'celebration'}
    added |= {'rarity_percent', 'rarity_tier', 'rarity_label', 'rarity_text'}
    assert set(badge) == legacy | added
    assert again.status_code == 201 and not _codes(again.data['new_badges'])


def test_badges_me_lists_earned_and_progress(api, user, make_recipe, make_meal_plan):
    make_meal_plan(user, recipes=[make_recipe(user, cuisine='italienne')], confirmed=True)

    resp = api.as_user(user).get('/api/badges/me/')

    assert resp.status_code == 200
    assert [b['code'] for b in resp.data['earned'] if b['code'] in HISTORIC] == ['first_cooked']  # rattrapé
    assert resp.data['total_count'] == len(BADGES)
    nxt = {b['code']: b['progress'] for b in resp.data['next']}
    assert nxt['cuisines_5'] == {'current': 1, 'target': 5, 'ratio': 0.2}
