"""
Carnet, filtre « Avec moi » (`?filter=with_me`, ckd2 lot 3 suite) : les plats des autres où le profil a été mis.

Jamais comptés comme ses plats (count, stats) ; posts publiés visibles par le lecteur seulement ; convive parti,
post privé, blocages exclus ; pagination ; `with_me_available` pour n'afficher le filtre que s'il sert.
"""

from datetime import date, timedelta

import pytest

from accounts.models import UserBlock
from recipes.models import MealInvitation, Post, PostRepost

pytestmark = pytest.mark.django_db

ME = '/api/carnet/me/'
WITH_ME = {'filter': 'with_me'}


def _user_url(u):
    return f'/api/carnet/users/{u.id}/'


def _keys(resp):
    return [e['key'] for e in resp.data['results']]


@pytest.fixture
def tom(make_user):
    return make_user('tom', first_name='Tom', avatar_url='https://cdn.test/tom.jpg')


@pytest.fixture
def lea(make_user):
    return make_user('lea', first_name='Léa')


def _hosted(
    host, guest, make_meal_plan, *, label='Gnocchis au pesto', day=date(2026, 11, 10), status='accepted', **post
):
    mp = make_meal_plan(host, day=day, meal_time='dinner', confirmed=True, custom_label=label)
    post.setdefault('is_published', True)
    p = Post.objects.create(user=host, meal_plan=mp, label=label, **post)
    MealInvitation.objects.create(inviter=host, invitee=guest, meal_plan=mp, status=status, source='direct')
    return p


def test_dishes_where_i_was_put_are_a_filter_not_my_entries(api, tom, lea, make_meal_plan):
    gnocchis = _hosted(tom, lea, make_meal_plan)
    client = api.as_user(lea)

    mine = client.get(ME)
    assert mine.status_code == 200
    assert mine.data['count'] == 0 and mine.data['results'] == []  # pas ses plats
    assert mine.data['with_me_available'] is True and mine.data['filter'] is None

    resp = client.get(ME, WITH_ME)
    assert resp.status_code == 200 and resp.data['filter'] == 'with_me'
    assert resp.data['count'] == 1 and _keys(resp) == [f'post-{gnocchis.id}']
    entry = resp.data['results'][0]
    assert entry['title'] == 'Gnocchis au pesto' and entry['date'] == '2026-11-10'
    assert entry['host'] == {
        'id': tom.id,
        'username': 'tom',
        'first_name': 'Tom',
        'avatar_url': 'https://cdn.test/tom.jpg',
    }
    assert entry['guests_count'] == 1 and entry['guests'][0]['id'] == lea.id
    # Le repas de l'hôte n'est pas le mien : ni batch, ni repas, ni portions.
    assert 'batch_id' not in entry and 'meal_plan_id' not in entry and 'servings' not in entry

    # Ni le compteur du profil, ni les stats.
    assert api.as_user(tom).get(f'/api/auth/users/{lea.id}/').data['cooked_count'] == 0


def test_filter_hidden_when_nothing_to_show(api, lea, make_meal_plan):
    make_meal_plan(lea, confirmed=True, custom_label='Restes')
    resp = api.as_user(lea).get(ME)
    assert resp.data['with_me_available'] is False
    empty = api.as_user(lea).get(ME, WITH_ME)
    assert empty.status_code == 200 and empty.data['count'] == 0 and empty.data['with_me_available'] is False


@pytest.mark.parametrize('case', ['left', 'pending', 'private', 'host_blocked', 'host_blocks_me', 'host_inactive'])
def test_excluded_cases(api, tom, lea, make_meal_plan, case):
    status = {'left': 'declined', 'pending': 'pending'}.get(case, 'accepted')
    post = _hosted(tom, lea, make_meal_plan, status=status, is_published=case != 'private')
    if case == 'host_blocked':
        UserBlock.objects.create(blocker=lea, blocked=tom)
    elif case == 'host_blocks_me':
        UserBlock.objects.create(blocker=tom, blocked=lea)
    elif case == 'host_inactive':
        tom.is_active = False
        tom.save(update_fields=['is_active'])
    resp = api.as_user(lea).get(ME, WITH_ME)
    assert resp.data['count'] == 0 and f'post-{post.id}' not in _keys(resp)
    assert api.as_user(lea).get(ME).data['with_me_available'] is False


def test_leaving_the_dish_removes_it_from_the_filter(api, tom, lea, make_meal_plan):
    post = _hosted(tom, lea, make_meal_plan)
    client = api.as_user(lea)
    assert client.get(ME, WITH_ME).data['count'] == 1
    assert client.post(f'/api/posts/{post.id}/leave/').status_code == 204
    assert client.get(ME, WITH_ME).data['count'] == 0


def test_reposted_dish_listed_once_and_own_posts_never(api, tom, lea, make_meal_plan):
    post = _hosted(tom, lea, make_meal_plan)
    PostRepost.objects.create(user=lea, original_post=post, host_user=tom)
    # Léa a aussi son propre post sur un repas où elle est (à tort) invitée : c'est son plat, pas « avec moi ».
    own = _hosted(lea, lea, make_meal_plan, label='Tarte')
    resp = api.as_user(lea).get(ME, WITH_ME)
    assert _keys(resp) == [f'post-{post.id}']
    assert f'post-{own.id}' in _keys(api.as_user(lea).get(ME))


def test_other_profile_filter_respects_reader_visibility(api, tom, lea, make_user, make_meal_plan, follow):
    reader = make_user('reader')
    pauline = make_user('pauline', first_name='Pauline')
    follow(reader, lea)  # le carnet de Léa est visible
    seen = _hosted(tom, lea, make_meal_plan, label='Gnocchis')
    follow(reader, tom)
    hidden = _hosted(pauline, lea, make_meal_plan, label='Raclette', day=date(2026, 11, 11))  # reader ne suit pas
    private = _hosted(tom, lea, make_meal_plan, label='Secret', day=date(2026, 11, 12), is_published=False)

    resp = api.as_user(reader).get(_user_url(lea), WITH_ME)
    assert resp.status_code == 200
    assert _keys(resp) == [f'post-{seen.id}']
    assert f'post-{hidden.id}' not in _keys(resp) and f'post-{private.id}' not in _keys(resp)
    assert api.as_user(reader).get(_user_url(lea)).data['with_me_available'] is True

    # Léa bloque Tom : ses plats ne sont plus montrés sur le carnet de Léa, même à un lecteur qui suit Tom.
    UserBlock.objects.create(blocker=lea, blocked=tom)
    assert api.as_user(reader).get(_user_url(lea), WITH_ME).data['count'] == 0

    # Profil non visible : 403 comme le carnet.
    stranger = make_user('stranger')
    assert api.as_user(stranger).get(_user_url(lea), WITH_ME).status_code == 403


def test_with_me_pagination_walks_every_post_once(api, lea, make_user, make_meal_plan):
    hosts = [make_user(f'host{i}') for i in range(3)]
    ids = set()
    for i in range(13):
        ids.add(_hosted(hosts[i % 3], lea, make_meal_plan, label=f'Plat {i}', day=date(2026, 10, 1) + timedelta(i)).id)
    client = api.as_user(lea)
    keys, cursor = [], None
    while True:
        params = {**WITH_ME, 'page_size': 5, **({'cursor': cursor} if cursor else {})}
        resp = client.get(ME, params)
        assert resp.status_code == 200 and resp.data['count'] == 13
        keys += _keys(resp)
        cursor = resp.data['next_cursor']
        if not cursor:
            break
    assert keys == [f'post-{i}' for i in sorted(ids, reverse=True)]


def test_unknown_filter_is_400(api, lea):
    resp = api.as_user(lea).get(ME, {'filter': 'nope'})
    assert resp.status_code == 400 and resp.data['code'] == 'invalid_filter'
