"""Sécurité : un id de recette privée d'un autre n'entre ni dans le panier ni dans « J'ai cuisiné »."""

from datetime import date

import pytest

from recipes.models import BasketItem, MealPlan, Post, Step

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def secret(make_user, make_recipe):
    owner = make_user('chef')
    recipe = make_recipe(owner, 'Sauce secrète', ingredients=['truffe'], is_public=False)
    Step.objects.create(recipe=recipe, order=1, instruction='Étape secrète')
    return recipe


def test_private_recipe_of_someone_else_is_not_usable(api, user, secret):
    client = api.as_user(user)
    resp = client.post('/api/basket/', {'recipe_id': secret.id}, format='json')
    assert resp.status_code == 404 and resp.data['code'] == 'recipe_not_found'
    assert not BasketItem.objects.filter(user=user).exists()

    for path in ('/api/posts/cooked/', '/api/posts/cooked/preview/'):
        resp = client.post(path, {'recipe_id': secret.id}, format='json')
        assert resp.status_code == 404, (path, resp.data)
        assert 'Sauce' not in str(resp.data)
    assert not MealPlan.objects.filter(user=user).exists()


def test_recipes_reached_legitimately_stay_usable(api, user, make_user, make_recipe, make_meal_plan, follow, secret):
    client = api.as_user(user)
    own_private = make_recipe(user, 'Ma recette', is_public=False)
    assert client.post('/api/basket/', {'recipe_id': own_private.id}, format='json').status_code == 201

    # « À mon tour » sur le post d'un ami dont la recette est privée, puis « J'ai cuisiné » depuis le panier.
    chef = secret.created_by
    follow(user, chef)
    mp = make_meal_plan(chef, day=date(2026, 11, 2), recipes=[secret], confirmed=True)
    post = Post.objects.create(
        user=chef, meal_plan=mp, recipe_batch=mp.meal_plan_recipe_batches.get().recipe_batch, is_published=True
    )
    assert client.post(f'/api/posts/{post.id}/my-turn/', format='json').status_code == 201
    assert client.post('/api/basket/', {'recipe_id': secret.id}, format='json').status_code == 201
    assert client.post('/api/posts/cooked/', {'recipe_id': secret.id}, format='json').status_code == 201
