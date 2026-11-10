"""
Carnet d'un profil (`GET /api/carnet/me/`, `GET /api/carnet/users/<id>/`, `recipes/services/carnet_service.py`) :
plats cuisinés avec ou sans recette, compte exact, pagination par curseur, visibilité stricte, requêtes constantes.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import FollowRequest, UserBlock
from recipes.models import MealPlan, MealPlanRecipeBatch, Post, PostPhoto, PostRepost, RecipeBatch

pytestmark = pytest.mark.django_db

ME = '/api/carnet/me/'


def _user_url(u):
    return f'/api/carnet/users/{u.id}/'


@pytest.fixture
def lea(make_user):
    return make_user('lea')


@pytest.fixture
def reader(make_user):
    return make_user('reader')


def _photo(post=None, *, meal_plan=None, recipe_batch=None, width=1080, height=1440, path='https://cdn.test/p.jpg'):
    return PostPhoto.objects.create(
        post=post,
        meal_plan=meal_plan,
        recipe_batch=recipe_batch,
        photo_type='final',
        image_path=path,
        width=width,
        height=height,
    )


@pytest.fixture
def lea_carnet(lea, make_meal_plan, make_recipe):
    """Le carnet de Léa : tous les cas (avec / sans recette, publié / privé, sans post, ancien batch, brouillon)."""
    quiche = make_recipe(lea, 'Quiche aux champignons')
    out = {}
    mp = make_meal_plan(lea, day=date(2026, 11, 9), meal_time='dinner', recipes=[quiche], confirmed=True)
    batch = mp.meal_plan_recipe_batches.first().recipe_batch
    out['published_recipe'] = Post.objects.create(
        user=lea, meal_plan=mp, recipe_batch=batch, is_published=True, comment='Trop bon'
    )
    _photo(out['published_recipe'])
    mp = make_meal_plan(
        lea, day=date(2026, 11, 7), meal_time='dinner', custom_label='Velouté de potimarron', confirmed=True
    )
    out['published_no_recipe'] = Post.objects.create(
        user=lea, meal_plan=mp, label='Velouté de potimarron', is_published=True
    )
    mp = make_meal_plan(lea, day=date(2026, 11, 5), confirmed=True, custom_label='Soupe secrète')
    out['private_post'] = Post.objects.create(user=lea, meal_plan=mp, label='Soupe secrète', is_published=False)
    out['meal_no_post_no_recipe'] = make_meal_plan(
        lea, day=date(2026, 11, 3), meal_time='lunch', custom_label='Riz aux aubergines', confirmed=True
    )
    _photo(meal_plan=out['meal_no_post_no_recipe'], width=800, height=600)
    out['meal_no_post_recipe'] = make_meal_plan(lea, day=date(2026, 11, 2), recipes=[quiche], confirmed=True)
    legacy = RecipeBatch.objects.create(recipe=quiche, created_by=lea, is_cooked=True)
    # Ancien batch sans repas : daté de sa création.
    RecipeBatch.objects.filter(pk=legacy.pk).update(created_at=datetime(2026, 10, 1, 12, tzinfo=UTC))
    out['legacy_batch'] = legacy
    # Pas cuisinés : repas prévu (non confirmé), brouillon de post sur un repas prévu.
    planned = make_meal_plan(lea, day=date(2026, 11, 20), custom_label='Prévu')
    Post.objects.create(user=lea, meal_plan=planned, label='Brouillon', is_published=False)
    return out


def _keys(resp):
    return [r['key'] for r in resp.data['results']]


# ---------------------------------------------------------------------------
# Soi
# ---------------------------------------------------------------------------
def test_self_carnet_has_every_cooked_dish_with_or_without_recipe(api, lea, lea_carnet):
    resp = api.as_user(lea).get(ME)
    assert resp.status_code == 200
    c = lea_carnet
    assert _keys(resp) == [
        f'post-{c["published_recipe"].id}',
        f'post-{c["published_no_recipe"].id}',
        f'post-{c["private_post"].id}',
        f'meal-{c["meal_no_post_no_recipe"].id}',
        f'meal-{c["meal_no_post_recipe"].id}',
        f'batch-{c["legacy_batch"].id}',
    ]
    assert resp.data['count'] == 6
    assert resp.data['next_cursor'] is None
    by_key = {r['key']: r for r in resp.data['results']}

    first = by_key[f'post-{c["published_recipe"].id}']
    assert first['title'] == 'Quiche aux champignons'
    assert first['recipe']['title'] == 'Quiche aux champignons'
    assert first['date'] == '2026-11-09'
    assert first['meal_time'] == 'dinner' and first['meal_time_display']
    assert first['thumbnail'] == {'url': 'https://cdn.test/p.jpg', 'width': 1080, 'height': 1440}
    assert first['photos_count'] == 1
    assert first['note'] == 'Trop bon'
    assert first['is_private'] is False
    assert first['batch_id'] == c['published_recipe'].recipe_batch_id
    assert first['servings'] >= 1

    velo = by_key[f'post-{c["published_no_recipe"].id}']
    assert velo['title'] == 'Velouté de potimarron'
    assert velo['recipe'] is None and velo['post_id'] == c['published_no_recipe'].id

    assert by_key[f'post-{c["private_post"].id}']['is_private'] is True

    riz = by_key[f'meal-{c["meal_no_post_no_recipe"].id}']
    assert riz['title'] == 'Riz aux aubergines'
    assert riz['recipe'] is None and riz['post_id'] is None and riz['batch_id'] is None
    assert riz['thumbnail'] == {'url': 'https://cdn.test/p.jpg', 'width': 800, 'height': 600}
    assert riz['is_private'] is True

    quiche_meal = by_key[f'meal-{c["meal_no_post_recipe"].id}']
    assert quiche_meal['recipe']['title'] == 'Quiche aux champignons' and quiche_meal['batch_id']

    legacy = by_key[f'batch-{c["legacy_batch"].id}']
    assert legacy['batch_id'] == c['legacy_batch'].id and legacy['recipe']['title'] == 'Quiche aux champignons'


def test_users_endpoint_on_self_is_my_carnet(api, lea, lea_carnet):
    client = api.as_user(lea)
    assert _keys(client.get(_user_url(lea))) == _keys(client.get(ME))


def test_self_count_matches_stats_cooked_count(api, lea, lea_carnet):
    """Un repas cuisiné = une entrée (avec ou sans recette, avec ou sans post), comme les stats."""
    from recipes.services.cooked_service import cooked_meal_plans

    data = api.as_user(lea).get(ME).data
    from_meals = [r for r in data['results'] if r['kind'] != 'batch']
    assert len(from_meals) == cooked_meal_plans(lea).count()


def test_first_time_flag_read_from_highlights_snapshot(api, lea, make_meal_plan):
    from stats.services.highlights import SNAPSHOT_VERSION

    mp = make_meal_plan(lea, confirmed=True, custom_label='Pho')
    Post.objects.create(
        user=lea,
        meal_plan=mp,
        label='Pho',
        is_published=True,
        highlights={'v': SNAPSHOT_VERSION, 'items': [{'kind': 'first_time', 'value': 1}], 'cuisine': None},
    )
    assert api.as_user(lea).get(ME).data['results'][0]['first_time'] is True


# ---------------------------------------------------------------------------
# Un autre : visibilité
# ---------------------------------------------------------------------------
def test_follower_sees_only_published_posts_including_no_recipe(api, lea, reader, lea_carnet, follow):
    follow(reader, lea)
    resp = api.as_user(reader).get(_user_url(lea))
    assert resp.status_code == 200
    c = lea_carnet
    assert _keys(resp) == [f'post-{c["published_recipe"].id}', f'post-{c["published_no_recipe"].id}']
    assert resp.data['count'] == 2
    for entry in resp.data['results']:
        assert entry['is_private'] is False
        # Jamais ses batches (quantités), ses repas ni son foyer (nombre de convives).
        assert 'batch_id' not in entry and 'meal_plan_id' not in entry and 'servings' not in entry
    assert resp.data['results'][1]['recipe'] is None
    assert resp.data['results'][1]['title'] == 'Velouté de potimarron'


def test_co_cooked_reposts_are_not_the_profile_owners_dishes(api, lea, reader, make_user, make_meal_plan, follow):
    host = make_user('host')
    mp = make_meal_plan(host, confirmed=True, custom_label='Raclette')
    hosts_post = Post.objects.create(user=host, meal_plan=mp, label='Raclette', is_published=True)
    PostRepost.objects.create(user=lea, original_post=hosts_post, host_user=host)
    follow(reader, lea)
    follow(reader, host)
    assert api.as_user(reader).get(_user_url(lea)).data['count'] == 0


@pytest.mark.parametrize('case', ['stranger', 'pending_request', 'followed_by_owner_only', 'blocked', 'blocker'])
def test_profile_not_visible_is_403_without_any_data(api, lea, reader, lea_carnet, follow, case):
    if case == 'pending_request':
        FollowRequest.objects.create(requester=reader, target=lea, status='pending')
    elif case == 'followed_by_owner_only':
        follow(lea, reader)
    elif case == 'blocked':
        follow(reader, lea)
        UserBlock.objects.create(blocker=lea, blocked=reader)
    elif case == 'blocker':
        follow(reader, lea)
        UserBlock.objects.create(blocker=reader, blocked=lea)
    resp = api.as_user(reader).get(_user_url(lea))
    assert resp.status_code == 403
    assert resp.data['code'] == 'profile_not_visible'
    assert 'results' not in resp.data and 'count' not in resp.data


def test_unknown_or_inactive_user_is_404(api, lea, reader, follow):
    follow(reader, lea)
    lea.is_active = False
    lea.save(update_fields=['is_active'])
    client = api.as_user(reader)
    assert client.get(_user_url(lea)).status_code == 404
    assert client.get('/api/carnet/users/999999/').status_code == 404


def test_anonymous_is_rejected(api, lea):
    assert api.get(_user_url(lea)).status_code in (401, 403)
    assert api.get(ME).status_code in (401, 403)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
def _cook_many(owner, make_meal_plan, n, *, start=date(2026, 10, 1)):
    posts = []
    for i in range(n):
        mp = make_meal_plan(owner, day=start + timedelta(days=i), confirmed=True, custom_label=f'Plat {i}')
        if i % 3:  # 2 sur 3 avec un post publié, le reste sans post
            posts.append(Post.objects.create(user=owner, meal_plan=mp, label=f'Plat {i}', is_published=True))
    return posts


def _walk(client, url, page_size):
    keys, cursor, pages = [], None, 0
    while True:
        params = {'page_size': page_size}
        if cursor:
            params['cursor'] = cursor
        resp = client.get(url, params)
        assert resp.status_code == 200
        keys += _keys(resp)
        pages += 1
        cursor = resp.data['next_cursor']
        if not cursor:
            return keys, pages, resp.data['count']


def test_pagination_walks_every_entry_once_newest_first(api, lea, reader, make_meal_plan, follow):
    _cook_many(lea, make_meal_plan, 32)
    keys, pages, count = _walk(api.as_user(lea), ME, 10)
    assert count == 32 and len(keys) == 32 and len(set(keys)) == 32 and pages == 4
    titles = [r['title'] for r in api.as_user(lea).get(ME, {'page_size': 50}).data['results']]
    assert titles == [f'Plat {i}' for i in reversed(range(32))]

    follow(reader, lea)
    keys, pages, count = _walk(api.as_user(reader), _user_url(lea), 7)
    published = Post.objects.filter(user=lea, is_published=True).count()
    assert count == published == len(keys) == len(set(keys))
    assert count > 20  # plus de la 1re page de /posts/ : plus de plafond à 20


def test_cursor_is_stable_when_a_dish_is_logged_while_scrolling(api, lea, make_meal_plan):
    _cook_many(lea, make_meal_plan, 6)
    client = api.as_user(lea)
    first = client.get(ME, {'page_size': 3})
    mp = make_meal_plan(lea, day=date(2026, 12, 1), confirmed=True, custom_label='Nouveau')
    second = client.get(ME, {'page_size': 3, 'cursor': first.data['next_cursor']})
    assert not set(_keys(first)) & set(_keys(second))
    assert f'meal-{mp.id}' not in _keys(second)
    assert second.data['count'] == 7


def test_invalid_cursor_is_400(api, lea):
    resp = api.as_user(lea).get(ME, {'cursor': 'pas-un-curseur'})
    assert resp.status_code == 400 and resp.data['code'] == 'invalid_cursor'


def test_page_size_is_capped(api, lea, make_meal_plan):
    _cook_many(lea, make_meal_plan, 55)
    resp = api.as_user(lea).get(ME, {'page_size': 500})
    assert len(resp.data['results']) == 50 and resp.data['next_cursor']


# ---------------------------------------------------------------------------
# Requêtes constantes
# ---------------------------------------------------------------------------
def _queries(client, url, page_size):
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(url, {'page_size': page_size})
    assert resp.status_code == 200
    return len(ctx.captured_queries), len(resp.data['results'])


def _rich_carnet(owner, make_meal_plan, make_recipe, n):
    recipe = make_recipe(owner, 'Dahl')
    for i in range(n):
        mp = make_meal_plan(owner, day=date(2026, 9, 1) + timedelta(days=i), confirmed=True, recipes=[recipe])
        batch = mp.meal_plan_recipe_batches.first().recipe_batch
        if i % 2:
            post = Post.objects.create(user=owner, meal_plan=mp, recipe_batch=batch, is_published=True)
            _photo(post)
        else:
            _photo(meal_plan=mp)
        RecipeBatch.objects.create(recipe=recipe, created_by=owner, is_cooked=True)


@pytest.mark.parametrize('viewer_is_owner', [True, False])
def test_query_count_does_not_grow_with_page_size(
    api, lea, reader, make_meal_plan, make_recipe, follow, viewer_is_owner
):
    _rich_carnet(lea, make_meal_plan, make_recipe, 24)
    follow(reader, lea)
    client = api.as_user(lea if viewer_is_owner else reader)
    url = ME if viewer_is_owner else _user_url(lea)
    # Les deux pages mêlent posts, repas sans post et anciens batches (soi) : seul leur nombre change.
    small, n_small = _queries(client, url, 30 if viewer_is_owner else 3)
    big, n_big = _queries(client, url, 48 if viewer_is_owner else 12)
    assert n_big > n_small
    assert big == small


def test_meal_without_recipe_batches_uses_its_label(api, lea, make_meal_plan):
    """Repas cuisiné sans aucune recette : titre = libellé du repas, pas de recette."""
    mp = make_meal_plan(lea, confirmed=True, custom_label='Restes')
    assert MealPlan.objects.filter(pk=mp.pk).exists()
    assert not MealPlanRecipeBatch.objects.filter(meal_plan=mp).exists()
    entry = api.as_user(lea).get(ME).data['results'][0]
    assert entry['title'] == 'Restes' and entry['recipe'] is None


def test_guests_and_guests_count_on_carnet(api, lea, reader, make_user, make_meal_plan, follow):
    from recipes.models import MealInvitation

    tom = make_user('tom', first_name='Tom', avatar_url='https://cdn.test/tom.jpg')
    pauline = make_user('pauline', first_name='Pauline', avatar_url='https://cdn.test/pauline.jpg')
    jules = make_user('jules', first_name='Jules')
    maxime = make_user('maxime', first_name='Maxime')
    pending_user = make_user('pending_user', first_name='Pending')

    # Léa cuisine un repas et invite 4 personnes (acceptées) + 1 en attente
    mp = make_meal_plan(lea, day=date(2026, 11, 8), confirmed=True, custom_label='Grand dîner')
    Post.objects.create(user=lea, meal_plan=mp, label='Grand dîner', is_published=True)

    MealInvitation.objects.create(inviter=lea, invitee=tom, meal_plan=mp, status='accepted')
    MealInvitation.objects.create(inviter=lea, invitee=pauline, meal_plan=mp, status='accepted')
    MealInvitation.objects.create(inviter=lea, invitee=jules, meal_plan=mp, status='accepted')
    MealInvitation.objects.create(inviter=lea, invitee=maxime, meal_plan=mp, status='accepted')
    MealInvitation.objects.create(inviter=lea, invitee=pending_user, meal_plan=mp, status='pending')

    # 1. Sur son propre carnet (Léa) : tous les invités acceptés (max 3) et guests_count exact
    lea_resp = api.as_user(lea).get(ME)
    entry = lea_resp.data['results'][0]
    assert entry['guests_count'] == 4
    assert len(entry['guests']) == 3
    assert [g['first_name'] for g in entry['guests']] == ['Tom', 'Pauline', 'Jules']
    assert entry['guests'][0]['avatar_url'] == 'https://cdn.test/tom.jpg'

    # 2. Sur le carnet d'un autre (reader) :
    # Reader suit Léa pour voir son carnet
    follow(reader, lea)
    # Reader ne suit que Tom parmi les invités
    follow(reader, tom)

    reader_resp = api.as_user(reader).get(_user_url(lea))
    reader_entry = reader_resp.data['results'][0]
    # guests_count reste le compte total d'invités
    assert reader_entry['guests_count'] == 4
    # Mais guests ne contient que les personnes visibles par le reader (Tom seul)
    assert len(reader_entry['guests']) == 1
    assert reader_entry['guests'][0]['first_name'] == 'Tom'
    assert reader_entry['guests'][0]['id'] == tom.id

    # 3. Si reader ne suit aucun invité : guests = [], guests_count = 4
    stranger = make_user('stranger')
    follow(stranger, lea)
    stranger_resp = api.as_user(stranger).get(_user_url(lea))
    stranger_entry = stranger_resp.data['results'][0]
    assert stranger_entry['guests_count'] == 4
    assert stranger_entry['guests'] == []
