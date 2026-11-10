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
    # Un autre client derrière le proxy (entrée ajoutée par Caddy à droite de X-Forwarded-For) n'est pas touché.
    relayed = APIClient(REMOTE_ADDR='198.51.100.1', HTTP_X_FORWARDED_FOR='192.0.2.55')
    assert relayed.get(f'/api/public/recipes/{recipe.id}/').status_code == 200


def test_client_ip_ignores_spoofed_leftmost_forwarded_for(make_user, make_recipe, settings):
    """Un client ne contourne pas la limite en préfixant X-Forwarded-For : seule l'entrée du proxy compte."""
    from core.client_ip import get_client_ip
    from recipes.views_public import PREVIEW_LIMIT_PER_IP

    settings.TRUSTED_PROXY_COUNT = 1
    recipe = make_recipe(make_user('lea'))
    url = f'/api/public/recipes/{recipe.id}/'
    statuses = [
        APIClient(REMOTE_ADDR='10.0.0.2', HTTP_X_FORWARDED_FOR=f'192.0.2.{n}, 203.0.113.9').get(url).status_code
        for n in range(PREVIEW_LIMIT_PER_IP + 1)
    ]
    assert statuses[-1] == 429
    assert set(statuses[:-1]) == {200}

    class _Req:
        def __init__(self, **meta):
            self.META = meta

    assert get_client_ip(_Req(REMOTE_ADDR='10.0.0.2', HTTP_X_FORWARDED_FOR='1.1.1.1, 203.0.113.9')) == '203.0.113.9'
    assert get_client_ip(_Req(REMOTE_ADDR='10.0.0.2', HTTP_X_FORWARDED_FOR='not-an-ip')) == '10.0.0.2'
    assert get_client_ip(_Req(REMOTE_ADDR='10.0.0.2')) == '10.0.0.2'
    settings.TRUSTED_PROXY_COUNT = 2
    assert get_client_ip(_Req(REMOTE_ADDR='10.0.0.2', HTTP_X_FORWARDED_FOR='1.1.1.1, 198.51.100.4, 10.0.0.9')) == (
        '198.51.100.4'
    )
    assert get_client_ip(_Req(REMOTE_ADDR='10.0.0.2', HTTP_X_FORWARDED_FOR='198.51.100.4')) == '10.0.0.2'
    settings.TRUSTED_PROXY_COUNT = 0
    assert get_client_ip(_Req(REMOTE_ADDR='10.0.0.2', HTTP_X_FORWARDED_FOR='198.51.100.4')) == '10.0.0.2'


def _site(key, visitor=None, ip='76.76.21.21'):
    """Requête de la fonction Vercel (toujours la même IP) relayant un visiteur."""
    headers = {'REMOTE_ADDR': ip, 'HTTP_X_TCHIKOOK_PREVIEW_KEY': key}
    if visitor:
        headers['HTTP_X_TCHIKOOK_VISITOR_IP'] = visitor
    return APIClient(**headers)


def test_site_preview_key_limits_per_visitor_not_per_vercel_ip(make_user, make_recipe, settings):
    from recipes.views_public import PREVIEW_LIMIT_PER_IP

    settings.PUBLIC_PREVIEW_SERVER_KEY = 'k' * 64
    recipe = make_recipe(make_user('lea'))
    url = f'/api/public/recipes/{recipe.id}/'

    # Beaucoup de visiteurs différents, une seule IP Vercel : aucune 429.
    statuses = {_site('k' * 64, f'192.0.2.{n}').get(url).status_code for n in range(PREVIEW_LIMIT_PER_IP + 10)}
    assert statuses == {200}
    # Un même visiteur relayé reste limité (compteur séparé de la limite par IP).
    visitor = _site('k' * 64, '198.51.100.77')
    assert {visitor.get(url).status_code for _ in range(PREVIEW_LIMIT_PER_IP)} == {200}
    assert visitor.get(url).status_code == 429
    # Clé fausse ou absente → limite par IP (l'IP Vercel a déjà servi plus de 60 fois sur un autre compteur).
    assert {
        _site('wrong', '192.0.2.1', ip='76.76.21.22').get(url).status_code for _ in range(PREVIEW_LIMIT_PER_IP)
    } == {200}
    assert _site('wrong', '192.0.2.1', ip='76.76.21.22').get(url).status_code == 429
    assert _site('', '192.0.2.2', ip='76.76.21.22').get(url).status_code == 429


def test_site_preview_key_global_cap_and_disabled_when_empty(make_user, make_recipe, settings):
    settings.PUBLIC_PREVIEW_SERVER_KEY = 'secret-key'
    settings.PUBLIC_PREVIEW_SERVER_LIMIT_PER_HOUR = 5
    recipe = make_recipe(make_user('lea'))
    url = f'/api/public/recipes/{recipe.id}/'
    assert {_site('secret-key', f'192.0.2.{n}').get(url).status_code for n in range(5)} == {200}
    assert _site('secret-key', '192.0.2.200').get(url).status_code == 429
    # Aperçu d'invitation : même plafond global de la clé.
    assert _site('secret-key', '192.0.2.201').get('/api/invites/ABCDEFGHJK/').status_code == 429

    # Clé vide côté serveur = désactivé : un en-tête vide ou quelconque ne donne aucun passe-droit.
    cache.clear()
    settings.PUBLIC_PREVIEW_SERVER_KEY = ''
    settings.PUBLIC_PREVIEW_SERVER_LIMIT_PER_HOUR = 5000
    from recipes.views_public import PREVIEW_LIMIT_PER_IP

    client = _site('', '192.0.2.9')
    assert {client.get(url).status_code for _ in range(PREVIEW_LIMIT_PER_IP)} == {200}
    assert client.get(url).status_code == 429


def test_invite_preview_uses_site_key_per_visitor(make_user, settings):
    from accounts.services.invite_service import get_or_create_active_code
    from accounts.views_invites import LOOKUP_LIMIT_PER_IP

    settings.PUBLIC_PREVIEW_SERVER_KEY = 'site-key'
    code = get_or_create_active_code(make_user('lea')).code
    url = f'/api/invites/{code}/'
    assert {_site('site-key', f'192.0.2.{n}').get(url).status_code for n in range(LOOKUP_LIMIT_PER_IP + 5)} == {200}
    visitor = _site('site-key', '198.51.100.8')
    assert {visitor.get(url).status_code for _ in range(LOOKUP_LIMIT_PER_IP)} == {200}
    assert visitor.get(url).status_code == 429
    # Sans clé, la même IP Vercel est limitée par IP.
    plain = APIClient(REMOTE_ADDR='76.76.21.30')
    assert {plain.get(url).status_code for _ in range(LOOKUP_LIMIT_PER_IP)} == {200}
    assert plain.get(url).status_code == 429


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
