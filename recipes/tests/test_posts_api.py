"""Posts : publication du « post du repas » et visibilité du feed social."""

from datetime import date

import pytest

from accounts.models import UserBlock
from recipes.models import Post, PostPhoto

pytestmark = pytest.mark.django_db


def _photo(meal_plan, owner, **extra):
    return PostPhoto.objects.create(
        meal_plan=meal_plan,
        uploaded_by=owner,
        photo_type='at_meal_time',
        image_path=f'meal_plans/{meal_plan.id}/photo.jpg',
        **extra,
    )


def _publish(client, meal_plan, **data):
    return client.post(f'/api/meal-plans/{meal_plan.id}/publish-post/', data, format='json')


def _published_post(author, make_meal_plan):
    mp = make_meal_plan(author, day=date(2026, 1, 5))
    return Post.objects.create(user=author, meal_plan=mp, is_published=True, comment=f'post de {author.username}')


def test_publish_meal_plan_post_without_recipe(api, user, make_meal_plan):
    mp = make_meal_plan(user)
    p1, p2 = _photo(mp, user), _photo(mp, user)
    client = api.as_user(user)

    resp = _publish(
        client, mp, photo_ids=[p2.id, p1.id], comment='Cantine du jour', meal_type='cantine', custom_label='Cantine'
    )

    assert resp.status_code == 201, resp.data
    post = Post.objects.get(id=resp.data['id'])
    assert post.is_published and post.user == user and post.meal_plan_id == mp.id
    assert list(post.photos.order_by('order').values_list('id', flat=True)) == [p2.id, p1.id]
    mp.refresh_from_db()
    assert mp.confirmed and mp.meal_type == 'cantine'

    # Un seul post publié par repas.
    assert _publish(client, mp, photo_ids=[p1.id], custom_label='x').status_code == 400


def test_publish_meal_plan_post_with_recipe_links_batch(api, user, make_meal_plan, make_recipe):
    mp = make_meal_plan(user)
    recipe = make_recipe(user)
    photo = _photo(mp, user)

    resp = _publish(api.as_user(user), mp, photo_ids=[photo.id], recipe_id=recipe.id)

    assert resp.status_code == 201, resp.data
    post = Post.objects.get(id=resp.data['id'])
    assert post.recipe_batch is not None and post.recipe_batch.recipe_id == recipe.id
    mp.refresh_from_db()
    assert mp.meal_type == 'recipe'


@pytest.mark.parametrize(
    ('payload', 'expected'),
    [
        ({'photo_ids': [], 'custom_label': 'Resto'}, 400),  # au moins une photo
        ({'photo_ids': 'nope', 'custom_label': 'Resto'}, 400),
        ({'photo_ids': ['__PHOTO__']}, 400),  # custom_label requis sans recette
        ({'photo_ids': ['__PHOTO__'], 'recipe_id': 999999}, 404),
    ],
)
def test_publish_meal_plan_post_validation(api, user, make_meal_plan, payload, expected):
    mp = make_meal_plan(user)
    photo = _photo(mp, user)
    payload = {
        k: ([photo.id if x == '__PHOTO__' else x for x in v] if isinstance(v, list) else v) for k, v in payload.items()
    }

    assert _publish(api.as_user(user), mp, **payload).status_code == expected
    assert not Post.objects.filter(meal_plan=mp).exists()


def test_cannot_publish_on_someone_else_meal_plan(api, user, make_user, make_meal_plan):
    owner = make_user()
    mp = make_meal_plan(owner)
    photo = _photo(mp, owner)

    resp = _publish(api.as_user(user), mp, photo_ids=[photo.id], custom_label='Hack')

    assert resp.status_code in (403, 404)
    assert not Post.objects.exists()


def test_feed_shows_self_and_followed_only_and_hides_blocked(api, user, make_user, make_meal_plan, follow):
    friend, blocked_friend, stranger = make_user('friend'), make_user('blocked'), make_user('stranger')
    follow(user, friend)
    follow(user, blocked_friend)
    follow(stranger, user)  # être suivi ne suffit pas à voir ses posts
    UserBlock.objects.create(blocker=blocked_friend, blocked=user)

    mine = _published_post(user, make_meal_plan)
    friends = _published_post(friend, make_meal_plan)
    hidden_blocked = _published_post(blocked_friend, make_meal_plan)
    hidden_stranger = _published_post(stranger, make_meal_plan)
    Post.objects.create(user=friend, is_published=False, comment='brouillon')

    client = api.as_user(user)
    resp = client.get('/api/posts/', {'is_published': 'true'})

    assert resp.status_code == 200
    ids = {p['id'] for p in resp.data['results']}
    assert ids == {mine.id, friends.id}

    assert client.get(f'/api/posts/{friends.id}/').status_code == 200
    assert client.get(f'/api/posts/{hidden_stranger.id}/').status_code == 404
    assert client.get(f'/api/posts/{hidden_blocked.id}/').status_code == 404
