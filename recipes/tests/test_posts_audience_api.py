"""« Qui voit ce plat ? » (ckd2 lot 4) : `POST /api/posts/{id}/audience/` — tes amis / tout Tchikook / toi seulement."""

import pytest

from accounts.models import Notification
from recipes.models import MealInvitation, Post

pytestmark = pytest.mark.django_db

COOKED_URL = '/api/posts/cooked/'
EVENING = '2026-11-08T20:15:00+01:00'


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def tom(make_user):
    return make_user('tom', first_name='Tom')


@pytest.fixture
def lea(make_user, follow, tom):
    lea = make_user('lea', first_name='Léa')
    follow(tom, lea)
    follow(lea, tom)
    return lea


@pytest.fixture
def stranger(make_user):
    return make_user('max', first_name='Max')


def _cook(api, user, capture, **data):
    data.setdefault('cooked_at', EVENING)
    with capture(execute=True):
        resp = api.as_user(user).post(COOKED_URL, data, format='json')
    assert resp.status_code == 201, resp.data
    return Post.objects.get(id=resp.data['id'])


def _audience(api, user, post, audience, capture=None):
    url = f'/api/posts/{post.id}/audience/'
    if capture is None:
        return api.as_user(user).post(url, {'audience': audience}, format='json')
    with capture(execute=True):
        return api.as_user(user).post(url, {'audience': audience}, format='json')


def test_friends_to_private_hides_the_post_from_everyone_else(
    api, tom, lea, stranger, make_recipe, django_capture_on_commit_callbacks
):
    post = _cook(api, tom, django_capture_on_commit_callbacks, recipe_id=make_recipe(tom).id, guest_ids=[lea.id])
    Post.objects.filter(pk=post.pk).update(is_public=True)
    assert api.as_user(lea).get(f'/api/posts/{post.id}/').status_code == 200

    resp = _audience(api, tom, post, 'private')

    assert resp.status_code == 200, resp.data
    assert resp.data['is_published'] is False and resp.data['is_public'] is False
    post.refresh_from_db()
    assert post.is_published is False and post.is_public is False
    # Ni l'amie convive, ni un inconnu (ancien partage communauté), ni le fil.
    assert api.as_user(lea).get(f'/api/posts/{post.id}/').status_code == 404
    assert api.as_user(stranger).get(f'/api/posts/{post.id}/').status_code == 404
    feed = api.as_user(lea).get('/api/posts/', {'is_published': 'true'})
    assert post.id not in [p['id'] for p in feed.data.get('results', feed.data)]
    profile = api.as_user(lea).get('/api/posts/', {'is_published': 'true', 'user': tom.id})
    assert post.id not in [p['id'] for p in profile.data.get('results', profile.data)]


def test_private_to_friends_publishes_and_applies_held_guests(
    api, tom, lea, make_recipe, django_capture_on_commit_callbacks
):
    post = _cook(
        api,
        tom,
        django_capture_on_commit_callbacks,
        recipe_id=make_recipe(tom, 'Quiche').id,
        guest_ids=[lea.id],
        publish=False,
    )
    assert post.held_guests == {'guest_ids': [lea.id], 'household_guest_ids': []}

    resp = _audience(api, tom, post, 'friends', django_capture_on_commit_callbacks)

    assert resp.status_code == 200, resp.data
    post.refresh_from_db()
    assert post.is_published is True and post.is_public is False
    assert post.held_guests is None
    assert MealInvitation.objects.filter(invitee=lea, status='accepted').exists()
    assert Notification.objects.filter(user=lea, notification_type='post_guest', related_post_id=post.id).exists()
    assert api.as_user(lea).get(f'/api/posts/{post.id}/').status_code == 200


def test_public_shares_with_the_community_and_back_to_friends(
    api, tom, stranger, make_recipe, django_capture_on_commit_callbacks
):
    post = _cook(api, tom, django_capture_on_commit_callbacks, recipe_id=make_recipe(tom).id, publish=False)

    resp = _audience(api, tom, post, 'public', django_capture_on_commit_callbacks)
    assert resp.status_code == 200, resp.data
    post.refresh_from_db()
    assert post.is_published is True and post.is_public is True
    assert api.as_user(stranger).get(f'/api/posts/{post.id}/').status_code == 200

    resp = _audience(api, tom, post, 'friends')
    assert resp.status_code == 200, resp.data
    post.refresh_from_db()
    assert post.is_published is True and post.is_public is False
    assert api.as_user(stranger).get(f'/api/posts/{post.id}/').status_code == 404


def test_only_the_author_with_a_known_audience(api, tom, lea, make_recipe, django_capture_on_commit_callbacks):
    post = _cook(api, tom, django_capture_on_commit_callbacks, recipe_id=make_recipe(tom).id)

    assert _audience(api, lea, post, 'private').status_code == 404
    bad = _audience(api, tom, post, 'everyone')
    assert bad.status_code == 400 and bad.data['code'] == 'invalid_audience'
    post.refresh_from_db()
    assert post.is_published is True
