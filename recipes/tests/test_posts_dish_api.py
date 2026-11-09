"""« Le carnet du plat » (page d'un plat) et « Classiques de tes amis » : identité, versions, amis, seuil, requêtes."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import UserBlock
from recipes.models import CookingProgress, Post, PostCookie, PostPhoto
from recipes.serializers import DishFieldMixin
from recipes.services.dish_service import dish_key, normalize_label, parse_dish_key, post_dish

pytestmark = pytest.mark.django_db

TODAY = date.today()
FEED_URL = '/api/posts/?is_published=true'


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def cook(make_meal_plan):
    """`cook(user, recipes=[…] | label=…, day=…)` : repas cuisiné + post publié."""

    def _cook(owner, *, recipes=(), label='', day=None, published=True, **post_extra):
        mp = make_meal_plan(
            owner, day=day or TODAY, meal_time='other', slot_key=uuid.uuid4().hex, recipes=list(recipes), confirmed=True
        )
        batch = mp.meal_plan_recipe_batches.first()
        return Post.objects.create(
            user=owner,
            meal_plan=mp,
            recipe_batch=batch.recipe_batch if batch else None,
            label=label,
            is_published=published,
            **post_extra,
        )

    return _cook


def test_dish_identity_recipe_first_then_normalized_label(user, make_recipe, cook):
    assert dish_key(42) == 'r-42'
    assert dish_key(None, '  ') is None
    same = {normalize_label(x) for x in ('Les pâtes au pesto', 'pâte au pesto', 'PÂTES AU PESTO !', 'pates  aux pesto')}
    assert same == {'pate-au-pesto'}
    assert normalize_label("L'omelette") == normalize_label('omelettes') == 'omelette'
    assert normalize_label('Risotto aux champignons') == normalize_label('risotto au champignon')
    assert parse_dish_key('l-pate-au-pesto').key == 'l-pate-au-pesto'
    assert parse_dish_key('R-7').recipe_id == 7

    risotto = make_recipe(user, 'Risotto')
    # Une recette l'emporte sur le libellé libre du post ; sans recette, libellé du post puis du repas.
    assert post_dish(cook(user, recipes=[risotto], label='Risotto de mamie')) == {
        'key': f'r-{risotto.id}',
        'title': 'Risotto',
        'recipe_id': risotto.id,
    }
    assert post_dish(cook(user, label='Les Pâtes au pesto'))['key'] == 'l-pate-au-pesto'


def test_my_versions_counts_favorite_duration_and_stats_link(user, make_user, make_recipe, cook, api):
    risotto, salad = make_recipe(user, 'Risotto aux champignons'), make_recipe(user, 'Salade')
    d1, d2, d3 = TODAY - timedelta(days=40), TODAY - timedelta(days=10), TODAY - timedelta(days=2)
    first = cook(user, recipes=[risotto], day=d1)
    liked = cook(user, recipes=[risotto], day=d2, comment='Plus de parmesan')
    combo = cook(user, recipes=[salad, risotto], day=d3)  # repas à 2 recettes : compte pour le risotto
    PostPhoto.objects.create(post=liked, photo_type='after_cooking', image_path='https://img/r.jpg')
    PostCookie.objects.create(post=liked, user=make_user())
    CookingProgress.objects.create(
        user=user, recipe_batch=first.recipe_batch, status='completed', total_time_minutes=40
    )
    CookingProgress.objects.create(
        user=user,
        recipe_batch=combo.meal_plan.meal_plan_recipe_batches.get(recipe_batch__recipe=risotto).recipe_batch,
        status='completed',
        total_time_minutes=50,
    )
    # Planifié (pas cuisiné) et plat libre homonyme : hors du plat « recette ».
    from recipes.models import MealPlan

    MealPlan.objects.filter(pk=cook(user, recipes=[risotto], day=TODAY).meal_plan_id).update(confirmed=False)
    cook(user, label='Risotto aux champignons', day=d3)

    client = api.as_user(user)
    data = client.get(f'/api/dishes/r-{risotto.id}/?page_size=2').json()
    assert data['dish'] == {'key': f'r-{risotto.id}', 'title': 'Risotto aux champignons', 'recipe_id': risotto.id}
    mine = data['mine']
    assert (mine['times_cooked'], mine['first_cooked_on'], mine['last_cooked_on']) == (
        3,
        d1.isoformat(),
        d3.isoformat(),
    )
    assert (mine['avg_cooking_minutes'], mine['measured_count']) == (45, 2)
    assert mine['favorite_meal_plan_id'] == liked.meal_plan_id
    versions = data['versions']
    assert (versions['count'], versions['has_next']) == (3, True)
    assert [v['nth_label'] for v in versions['results']] == ['3e fois', '2e fois']
    assert versions['results'][1]['post']['comment'] == 'Plus de parmesan'
    assert versions['results'][1]['post']['photo_url'] == 'https://img/r.jpg'
    assert data['image_url'] == 'https://img/r.jpg'
    page2 = client.get(f'/api/dishes/recipe/{risotto.id}/?page=2&page_size=2').json()['versions']['results']
    assert [v['nth_label'] for v in page2] == ['1re fois']
    assert data['basket']['body'] == {'recipe_id': risotto.id} and data['basket']['in_basket'] is False

    # Plat libre : texte libre normalisé (articles, accents, pluriels) ; inconnu → 404.
    cook(user, label='Les pâtes au pesto', day=d1)
    cook(user, label='pâte au pesto', day=d2)
    pesto = client.get('/api/dishes/label/Pâtes au pesto/').json()
    assert pesto['dish']['key'] == 'l-pate-au-pesto' and pesto['mine']['times_cooked'] == 2
    assert pesto['basket']['body'] == {'label': 'pâte au pesto'}
    assert client.get('/api/dishes/l-gratin-inconnu/').status_code == 404
    assert client.get('/api/dishes/n-importe-quoi/').status_code == 404

    # Stats : top_dishes porte la clé de la page.
    top = client.get('/api/stats/me/?period=all').json()['top_dishes']
    assert {d['dish_key'] for d in top} >= {f'r-{risotto.id}', 'l-pate-au-pesto'}


def test_friends_section_respects_visibility_and_blocks(user, make_user, make_recipe, cook, follow, api):
    risotto = make_recipe(user, 'Risotto')
    lea, tom, blocked, stranger = (make_user(n) for n in ('lea', 'tom', 'bloque', 'inconnu'))
    for friend in (lea, tom, blocked):
        follow(user, friend)
    cook(lea, recipes=[risotto], comment='Chez Léa')
    cook(lea, recipes=[risotto], day=TODAY - timedelta(days=60))
    cook(tom, recipes=[risotto], published=False)  # brouillon : invisible
    cook(blocked, recipes=[risotto])
    UserBlock.objects.create(blocker=blocked, blocked=user)  # bloqué dans l'autre sens : masqué aussi
    cook(stranger, recipes=[risotto], is_public=True)  # visible (communauté) mais pas un ami

    private = make_recipe(stranger, 'Secret', is_public=False)
    client = api.as_user(user)
    friends = client.get(f'/api/dishes/r-{risotto.id}/').json()['friends']
    assert friends['count'] == 1 and friends['posts_count'] == 2
    assert [u['user']['username'] for u in friends['users']] == ['lea']
    assert friends['users'][0]['times'] == 2
    assert friends['posts'][0]['comment'] == 'Chez Léa' and friends['posts'][0]['user']['username'] == 'lea'
    assert client.get(f'/api/dishes/r-{private.id}/').status_code == 404  # recette privée jamais croisée


def test_friends_classics_threshold_window_and_page_flag(user, make_user, make_recipe, cook, follow, api):
    risotto, quiche = make_recipe(user, 'Risotto'), make_recipe(user, 'Quiche')
    friends = [make_user(f'ami{i}') for i in range(4)]
    for friend in friends:
        follow(user, friend)
    client = api.as_user(user)
    assert client.get('/api/dishes/classics/').json()['results'] == []

    for friend in friends[:2]:
        cook(friend, recipes=[risotto], day=TODAY - timedelta(days=3))
        cook(friend, label='Les crêpes', day=TODAY - timedelta(days=1))
        cook(friend, recipes=[quiche], day=TODAY - timedelta(days=2))
    cook(friends[2], recipes=[risotto], day=TODAY - timedelta(days=45))  # hors fenêtre (30 j)
    cook(friends[2], label='crêpe', day=TODAY - timedelta(days=5))
    cook(friends[3], recipes=[quiche], day=TODAY - timedelta(days=4))
    cook(friends[3], label='crepes', day=TODAY - timedelta(days=6))

    cache.clear()
    data = client.get(f'/api/dishes/classics/?week={TODAY.isoformat()}').json()
    assert data['min_friends'] == 3 and data['window']['days'] == 30
    results = data['results']
    # Crêpes (4 amis) puis quiche (3) ; risotto : 2 amis dans la fenêtre seulement.
    assert [(r['dish']['key'], r['friends_count']) for r in results] == [('l-crepe', 4), (f'r-{quiche.id}', 3)]
    assert len(results[0]['friends']) == 4 and {'id', 'username', 'avatar_url'} <= set(results[0]['friends'][0])
    assert client.get(f'/api/dishes/r-{quiche.id}/').json()['classic']['is_friends_classic'] is True
    risotto_classic = client.get(f'/api/dishes/r-{risotto.id}/').json()['classic']
    assert (risotto_classic['is_friends_classic'], risotto_classic['friends_count']) == (False, 2)


def test_dish_page_query_count_is_constant(user, make_user, make_recipe, cook, follow, api):
    risotto = make_recipe(user, 'Risotto')
    client = api.as_user(user)
    url = f'/api/dishes/r-{risotto.id}/'

    def add(n_versions, n_friends):
        for i in range(n_versions):
            post = cook(user, recipes=[risotto], day=TODAY - timedelta(days=i + 1))
            PostPhoto.objects.create(post=post, photo_type='after_cooking', image_path=f'https://img/{post.id}.jpg')
        for _ in range(n_friends):
            friend = make_user()
            follow(user, friend)
            cook(friend, recipes=[risotto])

    def queries():
        client.get(url)  # ardoises calculées une fois ; on mesure à chaud
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(url).status_code == 200
        return len(ctx.captured_queries)

    add(1, 1)
    small = queries()
    add(4, 3)
    assert queries() == small


def test_feed_and_cooked_response_carry_dish_without_extra_queries(user, make_user, make_recipe, cook, follow, api):
    client = api.as_user(user)
    for i in range(3):
        friend = make_user()
        follow(user, friend)
        cook(friend, recipes=[make_recipe(friend, f'Plat {i}')])
        cook(friend, label='Les pâtes au pesto')

    def feed_queries():
        client.get(FEED_URL)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(FEED_URL)
        return resp.json()['results'], len(ctx.captured_queries)

    with mock.patch.object(DishFieldMixin, 'get_dish', return_value=None):
        _, without_dish = feed_queries()
    results, with_dish = feed_queries()
    assert with_dish == without_dish
    keys = [p['dish']['key'] for p in results]
    assert keys.count('l-pate-au-pesto') == 3 and sum(k.startswith('r-') for k in keys) == 3

    resp = client.post('/api/posts/cooked/', {'label': 'Pâtes au pesto'}, format='json')
    assert resp.status_code == 201 and resp.json()['dish']['key'] == 'l-pate-au-pesto'
    preview = client.post('/api/posts/cooked/preview/', {'label': 'pâte au pesto'}, format='json').json()
    assert preview['dish']['key'] == 'l-pate-au-pesto'
