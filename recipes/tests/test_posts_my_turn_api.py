"""« À mon tour » : ajout au panier depuis un post, annulation, notifications agrégées, cuisiné « d'après », feed sans N+1."""

import re
from datetime import date
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import Notification, PushDevice
from notifications.services import send_grouped_push
from recipes.models import BasketItem, Post

pytestmark = pytest.mark.django_db

FEED_URL = '/api/posts/?is_published=true'


@pytest.fixture
def expo():
    with mock.patch('notifications.services.send_expo_push_notifications') as send:
        send.messages = []
        send.side_effect = lambda msgs: send.messages.extend(list(msgs))
        yield send


@pytest.fixture
def quiche_post(user, make_recipe, make_meal_plan):
    recipe = make_recipe(user, 'Quiche', ingredients=['oeufs'])
    mp = make_meal_plan(user, day=date(2026, 11, 2), recipes=[recipe], confirmed=True)
    batch = mp.meal_plan_recipe_batches.get().recipe_batch
    return Post.objects.create(user=user, meal_plan=mp, recipe_batch=batch, is_published=True)


def _my_turn(client, post, method='post'):
    return getattr(client, method)(f'/api/posts/{post.id}/my-turn/', format='json')


def test_my_turn_add_undo_and_aggregated_notification(api, user, make_user, follow, quiche_post, expo, celery_delay):
    PushDevice.objects.create(user=user, expo_push_token='ExponentPushToken[alice]', platform='ios')
    lea, tom, zoe = (make_user(n) for n in ('lea', 'tom', 'zoe'))
    for fan in (lea, tom, zoe):
        follow(fan, user)

    resp = _my_turn(api.as_user(lea), quiche_post)
    assert resp.status_code == 201, resp.data
    assert resp.data['has_my_turn'] and resp.data['my_turn_count'] == 1 and resp.data['created']
    item = resp.data['basket_item']
    assert item['source'] == 'my_turn' and item['recipe']['title'] == 'Quiche' and item['status'] == 'todo'
    assert item['inspired_by']['post_id'] == quiche_post.id and item['inspired_by']['user']['id'] == user.id

    again = _my_turn(api.as_user(lea), quiche_post)  # double tap : idempotent
    assert again.status_code == 200 and not again.data['created'] and again.data['basket_item']['id'] == item['id']
    for fan in (tom, zoe):
        assert _my_turn(api.as_user(fan), quiche_post).status_code == 201

    # Une in-app par ami, push débouncée puis agrégée comme les miams.
    assert Notification.objects.filter(user=user, notification_type='a_mon_tour').count() == 3
    assert expo.messages == []
    assert {c[0] for c in celery_delay.calls} == {'notifications.tasks.send_grouped_push_task'}
    assert send_grouped_push(user.id, 'a_mon_tour', quiche_post.id).status == 'sent'
    (message,) = expo.messages
    assert message['title'] == 'Ton plat donne envie'
    assert message['body'] == 'zoe et 2 autres veulent refaire « Quiche ».'
    assert message['data']['type'] == 'a_mon_tour' and message['data']['post_id'] == quiche_post.id

    # Annuler : l'envie quitte le panier ; re-taper ne renotifie pas l'auteur.
    undo = _my_turn(api.as_user(lea), quiche_post, 'delete')
    assert undo.status_code == 200 and not undo.data['has_my_turn'] and undo.data['my_turn_count'] == 2
    assert not BasketItem.objects.filter(user=lea).exists()
    assert _my_turn(api.as_user(lea), quiche_post).status_code == 201
    assert Notification.objects.filter(user=user, notification_type='a_mon_tour').count() == 3

    # Post invisible (pas abonné) : 404 ; son propre post : ajout sans notification.
    assert _my_turn(api.as_user(make_user('stranger')), quiche_post).status_code == 404
    assert _my_turn(api.as_user(user), quiche_post).status_code == 201
    assert Notification.objects.filter(user=user, notification_type='a_mon_tour').count() == 3


def test_cooking_an_inspired_dish_links_the_post_and_tells_the_author(
    api, user, make_user, follow, quiche_post, expo, celery_delay
):
    lea = make_user('lea')
    follow(lea, user)
    follow(user, lea)
    lea_client = api.as_user(lea)
    item = _my_turn(lea_client, quiche_post).data['basket_item']

    cooked = lea_client.post('/api/posts/cooked/', {'basket_item_id': item['id']}, format='json')
    assert cooked.status_code == 201, cooked.data
    assert cooked.data['display_title'] == 'Quiche'
    assert cooked.data['inspired_by']['post_id'] == quiche_post.id
    assert cooked.data['inspired_by']['user']['id'] == user.id

    notif = Notification.objects.get(user=user, notification_type='a_mon_tour_cooked')
    assert notif.related_user_id == lea.id and notif.related_post_id == quiche_post.id
    assert notif.message == 'lea a refait « Quiche ».'

    # Feed de l'auteur : le post de Léa porte « d'après » ; l'envie cuisinée n'est plus « active ».
    feed = {p['id']: p for p in api.as_user(user).get(FEED_URL).data['results']}
    assert feed[cooked.data['id']]['inspired_by']['post_id'] == quiche_post.id
    assert feed[quiche_post.id]['my_turn_count'] == 1
    lea_feed = {p['id']: p for p in lea_client.get(FEED_URL).data['results']}
    assert lea_feed[quiche_post.id]['has_my_turn'] is False

    # Recuisiner (nouvelle envie) ne renotifie pas « a refait » pour le même post.
    item2 = _my_turn(lea_client, quiche_post).data['basket_item']
    cooked_at = '2026-11-01T12:00:00'
    lea_client.post('/api/posts/cooked/', {'basket_item_id': item2['id'], 'cooked_at': cooked_at}, format='json')
    assert Notification.objects.filter(user=user, notification_type='a_mon_tour_cooked').count() == 1


def test_feed_my_turn_fields_add_no_query(api, user, make_user, make_recipe, make_meal_plan, follow):
    """`my_turn_count` / `has_my_turn` / `inspired_by` : annotations et select_related, 0 requête par post."""
    client = api.as_user(user)
    fans = [make_user() for _ in range(2)]
    posts = []
    for i in range(6):
        author = make_user()
        follow(user, author)
        recipe = make_recipe(author, ingredients=['tomate'])
        mp = make_meal_plan(author, day=date(2026, 10, 5), meal_time='other', slot_key=f's{i}', recipes=[recipe])
        batch = mp.meal_plan_recipe_batches.get().recipe_batch
        posts.append(Post.objects.create(user=author, meal_plan=mp, recipe_batch=batch, is_published=True))

    def feed_queries():
        client.get(FEED_URL)  # instantanés « L'ardoise » calculés, on mesure à chaud
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(FEED_URL)
        sqls = [q['sql'] for q in ctx.captured_queries]
        # Les paniers ne sont lus qu'en sous-requête (alias U0…) de la requête des posts, jamais à part.
        assert not [q for q in sqls if re.search(r'FROM "recipes_basketitem"(?! U\d)', q)]
        return resp, len(sqls)

    _, before = feed_queries()
    for post in posts:
        for fan in fans:
            BasketItem.objects.create(user=fan, week_start=date(2026, 11, 2), label='x', inspired_by_post=post)
        BasketItem.objects.create(user=user, week_start=date(2026, 11, 2), label='x', inspired_by_post=post)
        Post.objects.filter(pk=post.pk).update(inspired_by_post=posts[0])
    resp, after = feed_queries()

    assert after == before, (before, after)
    results = resp.data['results']
    assert {p['my_turn_count'] for p in results} == {3}
    assert all(p['has_my_turn'] for p in results)
    assert {p['inspired_by']['post_id'] for p in results} == {posts[0].id}
