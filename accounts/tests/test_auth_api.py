"""Auth : inscription, login, JWT, access gate (waitlist) et paywall IA."""

import pytest
from rest_framework.test import APIClient

from accounts.models import AllowedEmail

pytestmark = pytest.mark.django_db

PROTECTED_URL = '/api/meal-plans/'


def _register(client, email='new@example.test', username='newbie', password='Sup3r-secret!'):
    return client.post(
        '/api/auth/register/',
        {'email': email, 'username': username, 'password': password},
        format='json',
    )


def _bearer(access):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
    return client


def test_register_returns_jwt_but_account_waits_for_validation():
    resp = _register(APIClient())

    assert resp.status_code == 201
    assert resp.data['user']['is_validated'] is False
    access = resp.data['tokens']['access']

    gated = _bearer(access).get(PROTECTED_URL)
    assert gated.status_code == 403
    assert gated.data['code'] == 'not_validated'

    # Le profil reste accessible (écran « en attente » côté app).
    assert _bearer(access).get('/api/auth/profile/').status_code == 200


def test_register_with_allowed_email_is_auto_validated():
    AllowedEmail.objects.create(email='VIP@Example.test')

    resp = _register(APIClient(), email='vip@example.test', username='vip')

    assert resp.status_code == 201
    assert resp.data['user']['is_validated'] is True
    assert _bearer(resp.data['tokens']['access']).get(PROTECTED_URL).status_code == 200


def test_register_rejects_duplicate_email_and_short_password(make_user):
    make_user('taken', email='taken@example.test')
    client = APIClient()

    assert _register(client, email='taken@example.test', username='other').status_code == 400
    assert _register(client, email='x@example.test', username='x', password='short').status_code == 400


def test_login_and_refresh_flow(make_user):
    make_user('bob', email='bob@example.test', password='Sup3r-secret!')
    client = APIClient()

    bad = client.post('/api/auth/login/', {'email': 'bob@example.test', 'password': 'nope'}, format='json')
    assert bad.status_code == 400

    ok = client.post('/api/auth/login/', {'email': 'bob@example.test', 'password': 'Sup3r-secret!'}, format='json')
    assert ok.status_code == 200
    tokens = ok.data['tokens']
    assert _bearer(tokens['access']).get(PROTECTED_URL).status_code == 200

    refreshed = client.post('/api/auth/token/refresh/', {'refresh': tokens['refresh']}, format='json')
    assert refreshed.status_code == 200
    assert _bearer(refreshed.data['access']).get(PROTECTED_URL).status_code == 200


def test_anonymous_and_invalid_token_are_rejected():
    assert APIClient().get(PROTECTED_URL).status_code == 401
    assert _bearer('not-a-jwt').get(PROTECTED_URL).status_code == 401


@pytest.mark.parametrize(
    ('paywall', 'plan', 'expected_status'),
    [
        (False, 'free', 202),
        (True, 'free', 403),
        (True, 'premium', 202),
    ],
)
def test_ai_feature_paywall(settings, api, make_user, celery_delay, paywall, plan, expected_status):
    settings.AI_PAYWALL_ENABLED = paywall
    u = make_user(plan=plan)

    resp = api.as_user(u).post(
        '/api/recipes/generate_from_idea/',
        {'idea_text': 'Un gratin de courgettes au chèvre', 'servings': 2},
        format='json',
    )

    assert resp.status_code == expected_status
    if expected_status == 403:
        assert resp.data['code'] == 'feature_locked'
        assert celery_delay.calls == []
    else:
        assert [c[0] for c in celery_delay.calls] == ['recipes.tasks.process_recipe_generate_from_idea']


def test_auto_publish_cooked_preference_round_trip(api, make_user):
    me, other = make_user(), make_user()
    client = api.as_user(me)

    initial = client.get('/api/auth/profile/').data
    assert initial['auto_publish_cooked'] is False and initial['auto_publish_cooked_set_at'] is None

    resp = client.patch('/api/auth/profile/', {'auto_publish_cooked': True}, format='json')
    assert resp.status_code == 200
    assert resp.data['auto_publish_cooked'] is True and resp.data['auto_publish_cooked_set_at']
    assert client.get('/api/auth/profile/').data['auto_publish_cooked'] is True

    # Préférence privée : absente du profil vu par un autre utilisateur.
    seen = api.as_user(other).get(f'/api/auth/users/{me.id}/')
    assert seen.status_code == 200 and 'auto_publish_cooked' not in seen.data


def test_register_and_profile_ignore_server_managed_fields(api, make_user):
    """Assignation de masse : niveau, XP, validation, offre ne se fixent ni à l'inscription ni via le profil."""
    resp = APIClient().post(
        '/api/auth/register/',
        {
            'email': 'mass@example.test',
            'username': 'mass',
            'password': 'Sup3r-secret!',
            'level': 99,
            'experience_points': 123456,
            'validated_at': '2026-01-01T00:00:00Z',
            'is_staff': True,
            'is_superuser': True,
            'plan': 'premium',
        },
        format='json',
    )
    assert resp.status_code == 201
    from django.contrib.auth import get_user_model

    created = get_user_model().objects.get(username='mass')
    assert (created.level, created.experience_points) == (1, 0)
    assert created.validated_at is None
    assert not created.is_staff and not created.is_superuser
    assert resp.data['user']['level'] == 1

    me = make_user('lea')
    original_plan = me.plan
    patch = api.as_user(me).patch(
        '/api/auth/profile/',
        {
            'level': 50,
            'experience_points': 9999,
            'validated_at': None,
            'plan': 'premium',
            'is_staff': True,
            'onboarding_completed': True,
        },
        format='json',
    )
    assert patch.status_code == 200
    me.refresh_from_db()
    assert (me.level, me.experience_points) == (1, 0)
    assert me.validated_at is not None
    assert me.plan == original_plan
    assert not me.is_staff
    assert me.onboarding_completed is True
