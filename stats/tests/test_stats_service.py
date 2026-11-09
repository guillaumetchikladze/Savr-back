"""Stats « cuisiné » : séries ISO (passage d'année), staples exclus, nouveaux ingrédients, plafonds doux, régalés."""

from datetime import date
from unittest import mock

import pytest

from recipes.models import Ingredient, MealInvitation, Post, PostCookie
from stats.models import IngredientProfile
from stats.services import stats_service
from stats.services.stats_service import compute_stats, get_stats_payload, resolve_period

pytestmark = pytest.mark.django_db

TODAY = date(2026, 11, 9)  # lundi, semaine ISO 2026-W46


@pytest.fixture
def cook(make_meal_plan):
    """`cook(user, day, *recipes, **extra)` : repas confirmé (= « cuisiné »)."""
    counter = iter(range(10_000))

    def _cook(owner, day, *recipes, **extra):
        extra.setdefault('slot_key', f'slot-{next(counter)}')
        extra.setdefault('meal_time', 'other')
        return make_meal_plan(owner, day=day, recipes=recipes, confirmed=True, **extra)

    return _cook


@pytest.fixture
def history(user, make_recipe, cook):
    ratatouille = make_recipe(user, 'Ratatouille', ingredients=['aubergine', 'tomate', 'sel', "huile d'olive"])
    dal = make_recipe(user, 'Dal', ingredients=['lentilles corail', 'oignon', 'curcuma'], cuisine='indienne')
    ramen = make_recipe(user, 'Ramen', ingredients=['nouilles', 'oignon', 'poivre'], cuisine='japonaise')
    # Profils explicites (les autres ingrédients retombent sur le seed déterministe).
    IngredientProfile.objects.create(
        ingredient=Ingredient.objects.get(name='curcuma'),
        food_group='herbes_epices',
        is_plant=True,
        is_basic_staple=True,
    )
    IngredientProfile.objects.create(
        ingredient=Ingredient.objects.get(name='aubergine'), food_group='legumes', is_plant=True
    )
    # Fin 2025 → début 2026 : semaines ISO 2025-W52, 2026-W01 (lundi 29/12/2025), 2026-W02.
    cook(user, date(2025, 12, 23), ratatouille)
    cook(user, date(2025, 12, 31), dal)
    cook(user, date(2026, 1, 6), ramen)
    # Série en cours : W44, W45 (rien encore en W46, semaine courante).
    cook(user, date(2026, 10, 27), dal)
    cook(user, date(2026, 11, 3), dal)
    return {'ratatouille': ratatouille, 'dal': dal, 'ramen': ramen}


def test_weekly_streak_spans_iso_year_boundary(user, history):
    all_time = compute_stats(user, resolve_period('all', today=TODAY), today=TODAY).payload
    assert all_time['weekly_streak_best'] == 3  # 2025-W52 → 2026-W01 → 2026-W02
    assert all_time['weekly_streak_current'] == 2  # W44, W45 ; la semaine en cours ne casse pas la série

    year = compute_stats(user, resolve_period('year', 2026, today=TODAY), today=TODAY).payload
    assert year['weekly_streak_best'] == 2  # dans 2026 : W02 seule, puis W44-W45
    assert year['weekly_streak_current'] == 2


def test_staples_excluded_and_new_ingredients(user, history):
    all_time = compute_stats(user, resolve_period('all', today=TODAY), today=TODAY).payload
    # sel / huile d'olive / poivre (seed) et curcuma (profil) exclus.
    assert all_time['distinct_ingredients'] == 5  # aubergine, tomate, lentilles corail, oignon, nouilles
    # Profils absents → seed déterministe (tomate, oignon, lentilles, nouilles sont végétaux).
    assert all_time['distinct_plants'] == 5
    assert [(g['code'], g['count']) for g in all_time['food_groups']] == [
        ('legumes', 3),
        ('legumineuses', 1),
        ('cereales_feculents', 1),
    ]

    year = compute_stats(user, resolve_period('year', 2026, today=TODAY), today=TODAY).payload
    # 2026 : dal (déjà cuisiné le 31/12/2025) + ramen → seules les nouilles sont nouvelles.
    assert year['distinct_ingredients'] == 3
    assert year['new_ingredients_in_period'] == 1
    assert year['new_ingredients_sample'] == ['nouilles']
    assert year['favorite_ingredient'] == {'id': Ingredient.objects.get(name='oignon').id, 'name': 'oignon', 'count': 3}
    assert [c['code'] for c in year['cuisines']] == ['indienne', 'japonaise']
    assert year['cuisines_count'] == 2
    assert year['star_dish']['label'] == 'Dal' and year['star_dish']['count'] == 2


def test_soft_caps_per_day(user, make_recipe, cook):
    soup = make_recipe(user, 'Soupe', ingredients=['poireau'])
    day = date(2026, 11, 2)
    for _ in range(6):
        cook(user, day, soup)

    stats = compute_stats(user, resolve_period('all', today=TODAY), today=TODAY).payload

    assert stats['cooked_count'] == 4  # MAX_COOKED_PER_DAY
    assert stats['cooked_days'] == 1
    assert stats['star_dish']['count'] == 1  # même plat le même jour : compté une fois


def test_hosted_excludes_self_and_household(user, make_user, make_recipe, cook, monkeypatch):
    friend, partner, other = make_user('friend'), make_user('partner'), make_user('other')
    meal = cook(user, date(2026, 11, 1), make_recipe(user), guest_count=40)
    for guest in (friend, partner):
        MealInvitation.objects.create(inviter=user, invitee=guest, meal_plan=meal, status='accepted')
    host_meal = cook(other, date(2026, 11, 2), make_recipe(other))
    MealInvitation.objects.create(inviter=other, invitee=user, meal_plan=host_meal, status='accepted')
    monkeypatch.setattr(stats_service, 'household_member_ids', lambda u: {partner.id})

    hosted = compute_stats(user, resolve_period('month', today=TODAY), today=TODAY).payload['hosted']

    assert hosted['guests_with_account'] == 1  # partner = foyer → exclu
    assert hosted['anonymous_guests'] == stats_service.MAX_ANON_GUESTS_PER_MEAL
    assert hosted['people_fed_count'] == 1 + stats_service.MAX_ANON_GUESTS_PER_MEAL
    assert hosted['top_guest']['username'] == 'friend'
    assert hosted['invited_count'] == 1


def test_minutes_miam_and_constant_query_count(user, make_user, history, cook, django_assert_max_num_queries):
    meal = cook(user, date(2026, 11, 4), history['ramen'])
    post = Post.objects.create(user=user, meal_plan=meal, is_published=True, cooking_time_minutes=45)
    PostCookie.objects.create(user=make_user('fan'), post=post)

    with django_assert_max_num_queries(12):
        stats = compute_stats(user, resolve_period('year', 2026, today=TODAY), today=TODAY).payload

    assert stats['total_cooking_minutes'] == 45
    assert stats['miam_received'] == 1
    assert stats['top_miam_post']['post_id'] == post.id
    assert stats['most_active_month'] == {'month': '2026-11', 'label': 'novembre', 'count': 2}


def test_cache_is_invalidated_by_new_cooked(user, history, cook):
    period = resolve_period('all', today=TODAY)
    first = get_stats_payload(user, period, today=TODAY)
    with mock.patch.object(stats_service, 'compute_stats', side_effect=AssertionError('cache miss')):
        assert get_stats_payload(user, period, today=TODAY) == first  # servi par le cache

    cook(user, date(2026, 11, 5), history['ramen'])

    assert get_stats_payload(user, period, today=TODAY)['cooked_count'] == first['cooked_count'] + 1
