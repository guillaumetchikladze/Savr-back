"""Aperçus publics des liens partagés (`/api/public/…`) et `share_url` (.claude-work/specs/deep-links.md §1)."""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from recipes.models import Post, PostPhoto, Step

pytestmark = pytest.mark.django_db

PRIVATE_MARKERS = ('example.test', 'email', 'allergies', 'food_dislikes', 'regimes', 'steps', 'Secret')


@pytest.fixture(autouse=True)
def _clear_rate_limits(settings):
    settings.PUBLIC_WEB_BASE_URL = 'https://tchikook.fr'
    cache.clear()
    yield
    cache.clear()


def _anon(ip='203.0.113.7'):
    return APIClient(REMOTE_ADDR=ip)


def _assert_no_private_data(payload):
    text = str(payload)
    for marker in PRIVATE_MARKERS:
        assert marker not in text, marker


# ---------------------------------------------------------------------------
# Recettes
# ---------------------------------------------------------------------------


def test_public_recipe_preview_is_minimal(make_user, make_recipe):
    author = make_user('lea', first_name='Léa', allergies=['arachide'])
    recipe = make_recipe(
        author,
        'Quiche lorraine',
        ingredients=['oeufs', 'lardons', 'crème', 'pâte brisée', 'gruyère', 'muscade', 'poivre'],
        image_path='https://cdn.example.org/quiche.jpg',
        description='Une quiche simple.',
    )
    Step.objects.create(recipe=recipe, order=1, instruction='Secret de grand-mère')

    resp = _anon().get(f'/api/public/recipes/{recipe.id}/')
    assert resp.status_code == 200, resp.data
    data = resp.data
    assert data['title'] == 'Quiche lorraine' and data['image_url'] == 'https://cdn.example.org/quiche.jpg'
    assert data['author'] == {'id': author.id, 'username': 'lea', 'display_name': 'Léa', 'avatar_url': None}
    assert (data['prep_time'], data['cook_time'], data['total_time'], data['servings']) == (10, 10, 20, 2)
    assert data['ingredients'] == ['oeufs', 'lardons', 'crème', 'pâte brisée', 'gruyère']
    assert data['has_more_ingredients'] is True
    assert data['url'] == f'https://tchikook.fr/r/{recipe.id}'
    assert data['app_url'] == f'tchikook://recipe/{recipe.id}'
    _assert_no_private_data(data)
    assert 'arachide' not in str(data)


def test_private_recipe_inactive_author_and_unknown_id_are_404(make_user, make_recipe, api):
    author = make_user('lea')
    private = make_recipe(author, 'Recette perso', is_public=False)
    gone = make_recipe(make_user('parti', is_active=False), 'Recette orpheline')

    for recipe_id in (private.id, gone.id, 999999):
        resp = _anon().get(f'/api/public/recipes/{recipe_id}/')
        assert resp.status_code == 404
        assert 'Recette' not in str(resp.data)

    # Même l'auteur connecté ne voit pas sa recette privée ici : le lecteur est toujours anonyme.
    assert api.as_user(author).get(f'/api/public/recipes/{private.id}/').status_code == 404
    # Un jeton invalide ne change rien (pas d'authentification sur ces routes).
    bad_token = APIClient(HTTP_AUTHORIZATION='Bearer nope')
    assert bad_token.get(f'/api/public/recipes/{make_recipe(author).id}/').status_code == 200


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


def test_public_post_preview(make_user, make_recipe, make_meal_plan):
    author = make_user('lea')
    recipe = make_recipe(author, 'Risotto')
    mp = make_meal_plan(author, recipes=[recipe], confirmed=True)
    post = Post.objects.create(
        user=author, meal_plan=mp, comment='Trop bon   ce soir', is_published=True, is_public=True
    )
    PostPhoto.objects.create(post=post, photo_type='after_cooking', image_path='https://cdn.example.org/r.jpg')

    data = _anon().get(f'/api/public/posts/{post.id}/').data
    assert data['teaser'] is False
    assert data['title'] == 'Risotto' and data['comment'] == 'Trop bon ce soir'
    assert data['image_url'] == 'https://cdn.example.org/r.jpg'
    assert data['author']['username'] == 'lea'
    assert data['recipe'] == {'id': recipe.id, 'title': 'Risotto', 'url': f'https://tchikook.fr/r/{recipe.id}'}
    assert data['url'] == f'https://tchikook.fr/p/{post.id}' and data['app_url'] == f'tchikook://post/{post.id}'
    _assert_no_private_data(data)


def test_public_post_never_links_a_private_recipe(make_user, make_recipe):
    from recipes.models import RecipeBatch

    author = make_user('lea')
    batch = RecipeBatch.objects.create(recipe=make_recipe(author, 'Perso', is_public=False), created_by=author)
    post = Post.objects.create(user=author, recipe_batch=batch, label='Gratin', is_published=True, is_public=True)

    data = _anon().get(f'/api/public/posts/{post.id}/').data
    assert data['title'] == 'Gratin' and data['recipe'] is None


def test_friends_only_post_is_a_teaser_without_content(make_user):
    author = make_user('lea')
    post = Post.objects.create(user=author, label='Secret', comment='Secret aussi', is_published=True)
    PostPhoto.objects.create(post=post, photo_type='after_cooking', image_path='https://cdn.example.org/secret.jpg')

    resp = _anon().get(f'/api/public/posts/{post.id}/')
    assert resp.status_code == 200
    assert resp.data == {
        'id': post.id,
        'url': f'https://tchikook.fr/p/{post.id}',
        'app_url': f'tchikook://post/{post.id}',
        'teaser': True,
        'message': 'Ouvre Tchikook pour voir ce plat.',
    }
    assert 'lea' not in str(resp.data) and 'cdn.example.org' not in str(resp.data)


def test_unpublished_or_inactive_author_post_is_404(make_user):
    draft = Post.objects.create(user=make_user('lea'), label='Brouillon', is_public=True, is_published=False)
    gone = Post.objects.create(
        user=make_user('parti', is_active=False), label='Ancien', is_public=True, is_published=True
    )
    for post_id in (draft.id, gone.id, 999999):
        assert _anon().get(f'/api/public/posts/{post_id}/').status_code == 404


# ---------------------------------------------------------------------------
# Profils
# ---------------------------------------------------------------------------


def test_public_profile_is_minimal(make_user, make_meal_plan):
    lea = make_user('Lea.Cuisine', first_name='Léa', allergies=['arachide'])
    make_meal_plan(lea, confirmed=True)
    make_meal_plan(lea, meal_time='dinner', confirmed=True)
    make_meal_plan(lea, meal_time='breakfast', confirmed=False)  # planifié, pas cuisiné

    resp = _anon().get('/api/public/users/lea.cuisine/')  # insensible à la casse
    assert resp.status_code == 200
    assert resp.data == {
        'id': lea.id,
        'username': 'Lea.Cuisine',
        'display_name': 'Léa',
        'avatar_url': None,
        'dishes_count': 2,
        'url': 'https://tchikook.fr/u/Lea.Cuisine',
        'app_url': f'tchikook://user/{lea.id}',
    }


def test_inactive_or_waitlisted_profile_is_404(make_user):
    make_user('parti', is_active=False)
    make_user('attente', validated=False)
    for name in ('parti', 'attente', 'inconnu'):
        assert _anon().get(f'/api/public/users/{name}/').status_code == 404


# ---------------------------------------------------------------------------
# Débit
# ---------------------------------------------------------------------------


def test_public_previews_are_rate_limited_per_ip(make_user, make_recipe):
    from recipes.views_public import PREVIEW_LIMIT_PER_IP

    recipe = make_recipe(make_user('lea'))
    client = _anon('198.51.100.1')
    statuses = [client.get(f'/api/public/recipes/{recipe.id}/').status_code for _ in range(PREVIEW_LIMIT_PER_IP)]
    assert set(statuses) == {200}
    # Les trois aperçus partagent le compteur.
    assert client.get('/api/public/users/lea/').status_code == 429
    assert client.get(f'/api/public/posts/{recipe.id}/').data['code'] == 'rate_limited'
    # Une autre IP (ex. visiteur relayé par le site via X-Forwarded-For) n'est pas touchée.
    relayed = APIClient(REMOTE_ADDR='198.51.100.1', HTTP_X_FORWARDED_FOR='192.0.2.55')
    assert relayed.get(f'/api/public/recipes/{recipe.id}/').status_code == 200


# ---------------------------------------------------------------------------
# share_url dans les serializers de l'app + aperçu d'invitation
# ---------------------------------------------------------------------------


def test_share_urls_in_app_serializers(api, make_user, make_recipe, settings):
    settings.PUBLIC_WEB_BASE_URL = 'https://preview.tchikook.fr'
    me = make_user('lea marie')
    recipe = make_recipe(me, 'Tarte')
    post = Post.objects.create(user=me, label='Tarte', is_published=True)
    client = api.as_user(me)

    assert client.get(f'/api/recipes/{recipe.id}/').data['share_url'] == f'https://preview.tchikook.fr/r/{recipe.id}'
    assert client.get(f'/api/posts/{post.id}/').data['share_url'] == f'https://preview.tchikook.fr/p/{post.id}'
    profile = client.get(f'/api/auth/users/{me.id}/').data
    assert profile['share_url'] == 'https://preview.tchikook.fr/u/lea%20marie'


def test_share_url_base_defaults_to_invite_domain(settings):
    from core.public_links import profile_share_url, recipe_share_url

    settings.PUBLIC_WEB_BASE_URL = ''
    settings.INVITE_BASE_URL = 'https://tchikook.fr/'
    assert recipe_share_url(7) == 'https://tchikook.fr/r/7'
    assert profile_share_url('a/b') == 'https://tchikook.fr/u/a%2Fb'


def test_invite_preview_exposes_display_name(api, make_user):
    lea = make_user('lea', first_name='Léa')
    code = api.as_user(lea).get('/api/invites/me/').data['code']
    inviter = _anon().get(f'/api/invites/{code}/').data['inviter']
    assert inviter['display_name'] == 'Léa' and 'email' not in inviter
