"""« L'ardoise » : puces du post (≤ 3, rareté d'abord), libellés FR, sémantique à la date du post, feed sans N+1, aperçu."""

import io
import re
from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from recipes.models import CookingProgress, MealInvitation, MealPlan, MealPlanRecipeBatch, Post, RecipeBatch
from stats.services.highlights import (
    KINDS,
    MAX_HIGHLIGHTS,
    duration_fr,
    make_highlight,
    ordinal_fr,
    rank_highlights,
)

pytestmark = pytest.mark.django_db

FEED_URL = '/api/posts/?is_published=true'
PREVIEW_URL = '/api/posts/cooked/preview/'
VEGGIES = ['tomate', 'courgette', 'aubergine', 'oignon', 'sel']  # 4 végétaux + 1 ingrédient de base


@pytest.fixture
def cooked_post(make_meal_plan):
    """`cooked_post(user, day, *recipes, **extra)` : repas confirmé + post publié (comme « J'ai cuisiné »)."""
    counter = iter(range(10_000))

    def _make(owner, day, *recipes, label='', **extra):
        extra.setdefault('slot_key', f'slot-{next(counter)}')
        mp = make_meal_plan(owner, day=day, meal_time='other', recipes=recipes, confirmed=True, **extra)
        first = mp.meal_plan_recipe_batches.order_by('order').first()
        return Post.objects.create(
            user=owner, meal_plan=mp, recipe_batch=first.recipe_batch if first else None, label=label, is_published=True
        )

    return _make


def _kinds(payload):
    return [h['kind'] for h in payload['highlights']]


# ---------------------------------------------------------------------------
# Libellés et classement (purs)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'kind,value,label',
    [
        ('first_time', 1, '1re fois'),
        ('nth_time', 2, '2e fois'),
        ('nth_time_quarter', 4, '4e fois ce trimestre'),
        ('streak', 1, "1re semaine d'affilée"),
        ('streak', 5, "5e semaine d'affilée"),
        ('new_ingredients', 1, '1 nouvel ingrédient'),
        ('new_ingredients', 3, '3 nouveaux ingrédients'),
        ('plants', 1, '1 végétal'),
        ('plants', 9, '9 végétaux'),
        ('guests', 1, '1 convive'),
        ('guests', 6, '6 convives'),
        ('duration', 45, '45 min'),
        ('duration', 60, '1 h'),
        ('duration', 95, '1 h 35'),
    ],
)
def test_french_labels(kind, value, label):
    assert make_highlight(kind, value)['label'] == label


def test_ordinals_and_durations():
    assert [ordinal_fr(n) for n in (1, 2, 3, 11)] == ['1re', '2e', '3e', '11e']
    assert ordinal_fr(1, feminine=False) == '1er'
    assert duration_fr(125) == '2 h 05'


def test_ranking_rarity_first_and_deterministic():
    candidates = [
        make_highlight('duration', 45),
        make_highlight('guests', 6),
        make_highlight('plants', 9),
        make_highlight('streak', 2),
        make_highlight('new_ingredients', 3),
        make_highlight('first_time', 1),
    ]
    ranked = rank_highlights(candidates)
    assert [h['kind'] for h in ranked] == ['first_time', 'new_ingredients', 'streak', 'plants', 'guests', 'duration']
    assert rank_highlights(list(reversed(candidates))) == ranked
    # Une série « palier » passe devant « ne fois ce trimestre », une série ordinaire derrière.
    assert rank_highlights([make_highlight('nth_time_quarter', 4), make_highlight('streak', 5)])[0]['kind'] == 'streak'
    assert rank_highlights([make_highlight('nth_time_quarter', 4), make_highlight('streak', 4)])[0]['kind'] == (
        'nth_time_quarter'
    )


def test_never_calories_or_health_scores():
    forbidden = re.compile(r'kcal|calor|santé|sante|nutri|score|gras|sucre', re.IGNORECASE)
    assert set(KINDS) == {
        'first_time',
        'nth_time',
        'nth_time_quarter',
        'new_ingredients',
        'plants',
        'guests',
        'duration',
        'streak',
    }
    for kind in KINDS:
        assert not forbidden.search(kind + make_highlight(kind, 3)['label'])


# ---------------------------------------------------------------------------
# Posts : ≤ 3 puces, à la date du post
# ---------------------------------------------------------------------------
def test_post_detail_max_three_chips_and_cuisine(api, user, make_user, make_recipe, cooked_post):
    risotto = make_recipe(user, 'Risotto', ingredients=VEGGIES, cuisine='italienne')
    post = cooked_post(user, date(2026, 10, 6), risotto, guest_count=3)
    MealInvitation.objects.create(inviter=user, invitee=make_user('ami'), meal_plan=post.meal_plan, status='accepted')
    CookingProgress.objects.create(user=user, recipe_batch=post.recipe_batch, status='completed', total_time_minutes=45)

    data = api.as_user(user).get(f'/api/posts/{post.id}/').data

    assert data['cuisine'] == {'code': 'italienne', 'label': 'Italienne', 'flag': '🇮🇹', 'country': 'IT'}
    assert len(data['highlights']) == MAX_HIGHLIGHTS
    assert _kinds(data) == ['first_time', 'new_ingredients', 'plants']
    assert data['highlights'][1] == {
        'kind': 'new_ingredients',
        'value': 4,  # « sel » = ingrédient de base, exclu
        'label': '4 nouveaux ingrédients',
        'icon': 'sprout',
    }
    # Les autres faits restent dans l'instantané (convives = 1 compte + 3 anonymes, durée mesurée).
    items = {h['kind']: h['label'] for h in Post.objects.get(pk=post.pk).highlights['items']}
    assert items['guests'] == '4 convives' and items['duration'] == '45 min'


def test_highlights_are_as_of_the_post_date(api, user, make_recipe, cooked_post):
    dal = make_recipe(user, 'Dal', ingredients=['lentilles corail', 'oignon'], cuisine='indienne')
    first = cooked_post(user, date(2026, 10, 5), dal)
    client = api.as_user(user)
    assert _kinds(client.get(f'/api/posts/{first.id}/').data)[0] == 'first_time'

    second = cooked_post(user, date(2026, 10, 12), dal)
    third = cooked_post(user, date(2026, 10, 19), dal)
    # Même recalculé de zéro (instantanés effacés), le 1er post reste « 1re fois ».
    Post.objects.update(highlights=None)
    by_id = {p['id']: p for p in client.get(FEED_URL).data['results']}

    assert by_id[first.id]['highlights'][0]['label'] == '1re fois'
    assert 'streak' not in _kinds(by_id[first.id])
    assert [h['label'] for h in by_id[second.id]['highlights']] == ['2e fois ce trimestre', "2e semaine d'affilée"]
    assert [h['label'] for h in by_id[third.id]['highlights']] == [
        "3e semaine d'affilée",  # palier
        '3e fois ce trimestre',
    ]


def test_snapshot_refreshed_when_guests_join(api, user, make_user, make_recipe, cooked_post):
    post = cooked_post(user, date(2026, 10, 5), make_recipe(user, 'Soupe'))
    client = api.as_user(user)
    assert 'guests' not in _kinds(client.get(f'/api/posts/{post.id}/').data)
    assert Post.objects.get(pk=post.pk).highlights is not None

    MealInvitation.objects.create(inviter=user, invitee=make_user('léa'), meal_plan=post.meal_plan, status='accepted')

    assert Post.objects.get(pk=post.pk).highlights is None
    labels = [h['label'] for h in client.get(f'/api/posts/{post.id}/').data['highlights']]
    assert labels == ['1re fois', '1 convive']


# ---------------------------------------------------------------------------
# Feed : pas de N+1 (instantané lu tel quel ; calcul à froid en nombre de requêtes constant)
# ---------------------------------------------------------------------------
def _feed_queries(client):
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(FEED_URL)
    assert resp.status_code == 200
    return resp, [q['sql'] for q in ctx.captured_queries]


def test_feed_query_count_is_bounded(api, user, make_user, make_recipe, cooked_post, follow):
    client = api.as_user(user)
    deltas = []
    for n_authors in (1, 6):
        Post.objects.all().delete()
        for _ in range(n_authors):
            author = make_user()
            follow(user, author)
            recipe = make_recipe(author, ingredients=['tomate', 'oignon'], cuisine='italienne')
            cooked_post(author, date(2026, 10, 5), recipe)
            cooked_post(author, date(2026, 10, 12), recipe)

        cold_resp, cold = _feed_queries(client)
        warm_resp, warm = _feed_queries(client)

        assert len(cold_resp.data['results']) == 2 * n_authors
        assert all(p['highlights'] for p in warm_resp.data['results'])
        assert warm_resp.data['results'] == cold_resp.data['results']
        # À chaud : aucune requête de l'ardoise (lecture de la colonne `highlights`).
        assert not [q for q in warm if 'recipes_recipeingredient' in q or 'recipes_cookingprogress' in q]
        deltas.append(len(cold) - len(warm))

    # À froid : surcoût constant (≤ 7 requêtes), indépendant du nombre d'auteurs et de posts.
    assert deltas[0] == deltas[1] and deltas[0] <= 7, deltas


# ---------------------------------------------------------------------------
# Aperçu « Ça donnera ça »
# ---------------------------------------------------------------------------
def test_preview_creates_nothing_and_matches_cooked(api, user, make_recipe, cooked_post):
    curry = make_recipe(user, 'Curry', ingredients=VEGGIES, cuisine='thai')
    cooked_post(user, date(2026, 10, 12), make_recipe(user, 'Pâtes', ingredients=['tomate']))
    body = {'recipe_id': curry.id, 'cooked_at': '2026-10-20T20:00:00+02:00'}
    client = api.as_user(user)
    models = (MealPlan, Post, RecipeBatch, MealPlanRecipeBatch)
    before = [m.objects.count() for m in models]

    preview = client.post(PREVIEW_URL, body, format='json')

    assert preview.status_code == 200, preview.data
    assert [m.objects.count() for m in models] == before
    assert preview.data['display_title'] == 'Curry'
    assert preview.data['cuisine']['code'] == 'thai'
    assert [h['label'] for h in preview.data['highlights']] == [
        '1re fois',
        '3 nouveaux ingrédients',  # tomate déjà cuisinée la semaine d'avant
        "2e semaine d'affilée",
    ]

    cooked = client.post('/api/posts/cooked/', body, format='json')
    assert cooked.status_code == 201
    assert cooked.data['highlights'] == preview.data['highlights']
    assert cooked.data['cuisine'] == preview.data['cuisine']


def test_preview_validation(api, user, make_user, make_meal_plan):
    client = api.as_user(user)
    assert client.post(PREVIEW_URL, {}, format='json').data['code'] == 'missing_dish'
    assert client.post(PREVIEW_URL, {'recipe_id': 999999}, format='json').status_code == 404
    other_meal = make_meal_plan(make_user('host'))
    MealInvitation.objects.create(inviter=other_meal.user, invitee=user, meal_plan=other_meal, status='accepted')
    assert client.post(PREVIEW_URL, {'meal_plan_id': other_meal.id}, format='json').status_code == 403
    free = client.post(PREVIEW_URL, {'label': 'Crêpes', 'cooked_at': '2026-10-20T09:00:00+02:00'}, format='json')
    assert free.status_code == 200
    assert free.data == {
        'display_title': 'Crêpes',
        'cuisine': None,
        'highlights': [{'kind': 'first_time', 'value': 1, 'label': '1re fois', 'icon': 'sparkles'}],
    }


def test_backfill_command(user, make_recipe, cooked_post):
    from django.core.management import call_command

    post = cooked_post(user, date(2026, 10, 5), make_recipe(user, 'Tarte'))
    stale = cooked_post(user, date(2026, 10, 6), make_recipe(user, 'Flan'))
    Post.objects.filter(pk=stale.pk).update(highlights={'v': 0, 'items': []})

    call_command('backfill_post_highlights', '--batch', '1', stdout=io.StringIO())

    assert all(
        p.highlights['v'] == 1 and p.highlights['items'] for p in Post.objects.filter(pk__in=[post.pk, stale.pk])
    )
