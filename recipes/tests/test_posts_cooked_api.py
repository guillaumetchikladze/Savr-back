"""Posts : « J'ai cuisiné » en 1 tap (POST /posts/cooked/), photos optionnelles, trace « cuisiné »."""

from datetime import date, time, timedelta
from unittest import mock

import pytest

from accounts.models import PushDevice, UserBlock
from notifications.models import NotificationPreference
from recipes.models import MealInvitation, MealPlan, Post, PostPhoto, RecipeBatch
from recipes.services.cooked_service import cooked_meal_plans

pytestmark = pytest.mark.django_db

COOKED_URL = '/api/posts/cooked/'
EVENING = '2026-11-08T20:15:00+01:00'


def _cooked(client, **data):
    return client.post(COOKED_URL, data, format='json')


def _photo(meal_plan, owner):
    return PostPhoto.objects.create(
        meal_plan=meal_plan, uploaded_by=owner, photo_type='at_meal_time', image_path=f'meal_plans/{meal_plan.id}/p.jpg'
    )


# ---------------------------------------------------------------------------
# Photo optionnelle + atomicité des endpoints existants
# ---------------------------------------------------------------------------
def test_meal_plan_publish_without_photo(api, user, make_meal_plan, make_recipe):
    mp = make_meal_plan(user, recipes=[make_recipe(user, 'Quiche')])

    resp = api.as_user(user).post(f'/api/meal-plans/{mp.id}/publish-post/', {'photo_ids': []}, format='json')

    assert resp.status_code == 201, resp.data
    assert resp.data['photo_ids'] == []
    mp.refresh_from_db()
    assert mp.confirmed
    assert Post.objects.get(id=resp.data['id']).photos.count() == 0


def test_meal_plan_publish_error_leaves_meal_plan_untouched(api, user, make_meal_plan, make_recipe):
    mp = make_meal_plan(user, meal_type='unknown')
    recipe = make_recipe(user)
    stranger_photo = PostPhoto.objects.create(photo_type='at_meal_time', image_path='x.jpg')

    resp = api.as_user(user).post(
        f'/api/meal-plans/{mp.id}/publish-post/',
        {'recipe_id': recipe.id, 'photo_ids': [stranger_photo.id]},
        format='json',
    )

    assert resp.status_code == 400
    mp.refresh_from_db()
    assert not mp.confirmed and mp.meal_type == 'unknown'
    assert not mp.meal_plan_recipe_batches.exists()
    assert not Post.objects.exists()


def test_legacy_batch_publish_without_photo_confirms_meal(api, user, make_meal_plan, make_recipe):
    mp = make_meal_plan(user, recipes=[make_recipe(user)])
    batch = mp.meal_plan_recipe_batches.get().recipe_batch

    resp = api.as_user(user).post(f'/api/recipe-batches/{batch.id}/publish-post/', {}, format='json')

    assert resp.status_code == 201, resp.data
    batch.refresh_from_db()
    mp.refresh_from_db()
    assert batch.is_cooked and mp.confirmed


# ---------------------------------------------------------------------------
# POST /posts/cooked/
# ---------------------------------------------------------------------------
def test_cooked_with_recipe_creates_confirmed_meal_and_feed_post(api, user, make_recipe):
    recipe = make_recipe(user, 'Quiche aux champignons')

    resp = _cooked(api.as_user(user), recipe_id=recipe.id, cooked_at=EVENING)

    assert resp.status_code == 201, resp.data
    assert resp.data['created'] is True
    assert resp.data['display_title'] == 'Quiche aux champignons'
    assert resp.data['photos'] == [] and resp.data['is_published'] is True
    assert resp.data['meal_plan']['date'] == '2026-11-08' and resp.data['meal_plan']['meal_time'] == 'dinner'
    mp = MealPlan.objects.get(id=resp.data['meal_plan']['id'])
    assert mp.confirmed and mp.user == user
    batch = mp.meal_plan_recipe_batches.get().recipe_batch
    assert batch.recipe_id == recipe.id and batch.is_cooked


def test_cooked_with_label_only_and_private(api, user):
    resp = _cooked(api.as_user(user), label='Pâtes au pesto', publish=False, cooked_at='2026-11-08T12:30:00+01:00')

    assert resp.status_code == 201, resp.data
    assert resp.data['display_title'] == 'Pâtes au pesto' and resp.data['label'] == 'Pâtes au pesto'
    assert resp.data['is_published'] is False
    mp = MealPlan.objects.get(id=resp.data['meal_plan']['id'])
    # Les clients ≤ 1.0.8 lisent meal_plan.custom_label pour un post sans recette.
    assert mp.meal_time == 'lunch' and mp.custom_label == 'Pâtes au pesto' and mp.confirmed


def test_cooked_reuses_planned_meal_and_falls_back_to_other_slot(api, user, make_meal_plan, make_recipe):
    quiche, risotto = make_recipe(user, 'Quiche'), make_recipe(user, 'Risotto')
    planned = make_meal_plan(user, day=date(2026, 11, 8), meal_time='dinner', recipes=[quiche])
    client = api.as_user(user)

    reused = _cooked(client, recipe_id=quiche.id, cooked_at=EVENING)
    other = _cooked(client, recipe_id=risotto.id, cooked_at=EVENING)

    assert reused.status_code == 201 and reused.data['meal_plan']['id'] == planned.id
    assert other.status_code == 201 and other.data['meal_plan']['meal_time'] == 'other'
    planned.refresh_from_db()
    assert planned.confirmed and planned.meal_plan_recipe_batches.count() == 1


def test_cooked_with_existing_meal_plan_and_photo(api, user, make_meal_plan, make_recipe):
    mp = make_meal_plan(user, recipes=[make_recipe(user, 'Tarte'), make_recipe(user, 'Salade')])
    photo = _photo(mp, user)

    resp = _cooked(api.as_user(user), meal_plan_id=mp.id, photo_ids=[photo.id])

    assert resp.status_code == 201, resp.data
    assert resp.data['display_title'] == 'Tarte et Salade'
    assert [p['id'] for p in resp.data['photos']] == [photo.id]
    assert all(b.is_cooked for b in RecipeBatch.objects.filter(meal_plan_recipe_batches__meal_plan=mp))


def test_cooked_is_idempotent(api, user, make_meal_plan, make_recipe):
    recipe = make_recipe(user)
    mp = make_meal_plan(user)
    client = api.as_user(user)

    first = _cooked(client, meal_plan_id=mp.id, recipe_id=recipe.id)
    again = _cooked(client, meal_plan_id=mp.id)
    same_day = _cooked(client, recipe_id=recipe.id)  # double tap sans meal_plan_id
    keyed = client.post(COOKED_URL, {'label': 'Soupe'}, format='json', HTTP_IDEMPOTENCY_KEY='k1')
    keyed_retry = client.post(COOKED_URL, {'label': 'Autre'}, format='json', HTTP_IDEMPOTENCY_KEY='k1')

    assert first.status_code == 201
    assert again.status_code == 200 and again.data['created'] is False and again.data['id'] == first.data['id']
    assert same_day.status_code == 200 and same_day.data['id'] == first.data['id']
    assert keyed.status_code == 201 and keyed_retry.status_code == 200
    assert keyed_retry.data['id'] == keyed.data['id']
    assert Post.objects.count() == 2


@pytest.mark.parametrize(
    ('payload', 'expected'),
    [
        ({}, 400),
        ({'recipe_id': 999999}, 404),
        ({'label': 'x', 'cooked_at': 'demain'}, 400),
        ({'label': 'x', 'photo_ids': 'nope'}, 400),
    ],
)
def test_cooked_validation(api, user, payload, expected):
    assert _cooked(api.as_user(user), **payload).status_code == expected
    assert not Post.objects.exists() and not MealPlan.objects.exists()


def test_cooked_permissions_on_meal_plan(api, user, make_user, make_meal_plan):
    host, stranger = make_user('host'), make_user('stranger')
    mp = make_meal_plan(host)
    MealInvitation.objects.create(inviter=host, invitee=user, meal_plan=mp, status='accepted')

    assert _cooked(api.as_user(user), meal_plan_id=mp.id, label='x').status_code == 403
    assert _cooked(api.as_user(stranger), meal_plan_id=mp.id, label='x').status_code == 404
    mp.refresh_from_db()
    assert not mp.confirmed and not Post.objects.exists()


def test_undo_cooked_restores_state(api, user, make_meal_plan, make_recipe):
    recipe = make_recipe(user)
    planned = make_meal_plan(user, day=date(2026, 11, 8), meal_time='dinner', recipes=[recipe])
    client = api.as_user(user)

    created = _cooked(client, label='Gratin', cooked_at='2026-11-08T08:00:00+01:00')
    reused = _cooked(client, recipe_id=recipe.id, cooked_at=EVENING)

    assert client.delete(f'/api/posts/{created.data["id"]}/?undo_cooked=true').status_code == 204
    assert client.delete(f'/api/posts/{reused.data["id"]}/?undo_cooked=true').status_code == 204
    assert not Post.objects.exists()
    assert list(MealPlan.objects.values_list('id', flat=True)) == [planned.id]  # repas créé supprimé
    planned.refresh_from_db()
    assert not planned.confirmed
    assert not planned.meal_plan_recipe_batches.get().recipe_batch.is_cooked


def test_add_photos_to_cooked_post(api, user, make_user, make_recipe):
    client = api.as_user(user)
    post_id = _cooked(client, recipe_id=make_recipe(user).id).data['id']
    mp_id = Post.objects.get(id=post_id).meal_plan_id

    ok = client.post(f'/api/posts/{post_id}/photos/', {'image_paths': [f'meal_plans/{mp_id}/a.jpg']}, format='json')
    bad_path = client.post(f'/api/posts/{post_id}/photos/', {'image_paths': ['avatars/x.jpg']}, format='json')
    other = api.as_user(make_user()).post(f'/api/posts/{post_id}/photos/', {'image_paths': []}, format='json')

    assert ok.status_code == 201 and len(ok.data['photos']) == 1
    assert bad_path.status_code == 400
    assert other.status_code == 404


def test_feed_shows_photoless_cooked_post_to_followers_not_blocked(api, user, make_user, make_recipe, follow):
    friend, blocked = make_user('friend'), make_user('blocked')
    follow(friend, user)
    follow(blocked, user)
    UserBlock.objects.create(blocker=user, blocked=blocked)
    post_id = _cooked(api.as_user(user), recipe_id=make_recipe(user, 'Quiche').id).data['id']

    friend_feed = api.as_user(friend).get('/api/posts/', {'is_published': 'true'}).data['results']
    blocked_feed = api.as_user(blocked).get('/api/posts/', {'is_published': 'true'}).data['results']

    entry = next(p for p in friend_feed if p['id'] == post_id)
    assert entry['photos'] == [] and entry['display_title'] == 'Quiche'
    assert post_id not in {p['id'] for p in blocked_feed}


# ---------------------------------------------------------------------------
# Trace « cuisiné » (stats) + mode cuisine + rappel push
# ---------------------------------------------------------------------------
def test_cooked_meal_plans_helper(api, user, make_user, make_meal_plan, make_recipe):
    other = make_user()
    today = date.today()
    cooked = make_meal_plan(user, day=today, confirmed=True)
    make_meal_plan(user, day=today, meal_time='dinner')  # planifié, pas cuisiné
    make_meal_plan(other, day=today, confirmed=True)
    old = make_meal_plan(user, day=today - timedelta(days=30), confirmed=True)
    via_cooking_mode = make_meal_plan(user, day=today, meal_time='breakfast', recipes=[make_recipe(user)])
    batch = via_cooking_mode.meal_plan_recipe_batches.get().recipe_batch

    assert api.as_user(user).post(f'/api/recipe-batches/{batch.id}/complete_cooking/').status_code == 200

    assert set(cooked_meal_plans(user)) == {cooked, old, via_cooking_mode}
    assert set(cooked_meal_plans(user, start=today - timedelta(days=7), end=today)) == {cooked, via_cooking_mode}


def test_meal_time_reminder_push_payload(user, make_meal_plan, make_recipe):
    from accounts.tasks import send_meal_time_photo_reminder_push

    recipe = make_recipe(user, 'Quiche')
    mp = make_meal_plan(user, day=date(2026, 11, 8), meal_time='dinner', recipes=[recipe])
    PushDevice.objects.create(user=user, expo_push_token='ExponentPushToken[x]', platform='ios')
    # Heures calmes désactivées (début == fin) : le test ne dépend pas de l'heure d'exécution.
    NotificationPreference.objects.create(user=user, quiet_hours_start=time(8), quiet_hours_end=time(8))

    with mock.patch('notifications.services.send_expo_push_notifications') as send:
        send_meal_time_photo_reminder_push(user.id, mp.id)

    (message,) = send.call_args[0][0]
    assert message['body'] == 'Tu as cuisiné Quiche ? Partage-le en 1 tap.'
    assert message['categoryId'] == 'cooked_prompt' and message['data']['type'] == 'cooked_prompt'
    assert message['data']['kind'] == 'meal_time_photo_reminder' and message['data']['meal_plan_id'] == mp.id
    assert message['data']['title'] == 'Quiche' and message['data']['recipe_ids'] == [recipe.id]
    assert message['data']['action'] == 'cooked_prompt'
