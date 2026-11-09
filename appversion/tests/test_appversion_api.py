"""Contrôle de version de l'app : règles, semver, fail-open, accès public, middleware 426."""

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from appversion.models import AppVersionPolicy
from appversion.versioning import parse_version

pytestmark = pytest.mark.django_db

URL = '/api/app/version-check/'
PROTECTED_URL = '/api/meal-plans/'


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def policy():
    """iOS : latest 1.0.10, min 1.0.8 (la ligne seedée par la migration est mise à jour)."""
    obj, _ = AppVersionPolicy.objects.update_or_create(
        platform='ios',
        defaults={
            'latest_version': '1.0.10',
            'min_supported_version': '1.0.8',
            'store_url': 'https://apps.apple.com/app/id1',
            'message_update_available': 'Nouvelle version dispo',
            'message_update_required': 'Mise à jour obligatoire',
        },
    )
    return obj


def _check(client=None, **params):
    return (client or APIClient()).get(URL, params)


# --- semver -----------------------------------------------------------------


@pytest.mark.parametrize(
    'a, b',
    [('1.0.10', '1.0.9'), ('1.1', '1.0.99'), ('2', '1.9.9'), ('1.0.0.1', '1.0')],
)
def test_parse_version_orders_numerically(a, b):
    assert parse_version(a) > parse_version(b)


@pytest.mark.parametrize('raw', ['1.0', '1.0.0', '1', ' v1.0.0 ', '1.0.0-beta', '1.0+42', '1.0.0 (42)'])
def test_parse_version_normalizes_and_ignores_suffixes(raw):
    assert parse_version(raw) == parse_version('1.0.0') == (1,)


@pytest.mark.parametrize('raw', [None, '', 'abc', 'beta-1.0', '.1', '1' * 100, '1.2.3.4.5.6', 42])
def test_parse_version_rejects_junk(raw):
    assert parse_version(raw) is None


# --- règles -----------------------------------------------------------------


@pytest.mark.parametrize(
    'version, status, message',
    [
        ('1.0.7', 'update_required', 'Mise à jour obligatoire'),
        ('1.0.8', 'update_available', 'Nouvelle version dispo'),
        ('1.0.9', 'update_available', 'Nouvelle version dispo'),
        ('1.0.10', 'ok', None),
        ('1.0.10-beta', 'ok', None),
        ('1.1', 'ok', None),
    ],
)
def test_status_rules(policy, version, status, message):
    resp = _check(platform='ios', version=version, build='42')

    assert resp.status_code == 200
    assert resp.json() == {
        'status': status,
        'latest_version': '1.0.10',
        'min_supported_version': '1.0.8',
        'store_url': 'https://apps.apple.com/app/id1',
        'message': message,
    }
    assert 'max-age=300' in resp['Cache-Control']


def test_seeded_policies_exist_for_both_platforms():
    rows = {p.platform: p for p in AppVersionPolicy.objects.all()}

    assert set(rows) == {'ios', 'android'}
    assert rows['android'].store_url == 'https://play.google.com/store/apps/details?id=com.tchikook.app'
    assert _check(platform='Android', version='1.0.8').json()['status'] == 'ok'


@pytest.mark.parametrize(
    'params',
    [{}, {'platform': 'windows', 'version': '0.1'}, {'platform': 'ios'}, {'platform': 'ios', 'version': 'n/a'}],
)
def test_junk_input_fails_open(policy, params):
    resp = _check(**params)

    assert resp.status_code == 200
    assert resp.json()['status'] == 'ok'


def test_missing_policy_falls_back_to_settings_then_ok(settings):
    AppVersionPolicy.objects.all().delete()
    assert _check(platform='ios', version='0.1').json()['status'] == 'ok'

    cache.clear()
    settings.APP_LATEST_VERSION_IOS = '2.0.0'
    settings.APP_MIN_SUPPORTED_VERSION_IOS = '1.5'
    data = _check(platform='ios', version='1.0.9').json()
    assert data['status'] == 'update_required'
    assert data['min_supported_version'] == '1.5'


def test_saving_policy_invalidates_cache(policy):
    assert _check(platform='ios', version='1.0.8').json()['status'] == 'update_available'

    policy.min_supported_version = '1.0.9'
    policy.save()

    assert _check(platform='ios', version='1.0.8').json()['status'] == 'update_required'


def test_model_validation_rejects_min_above_latest(policy):
    policy.min_supported_version = '1.0.11'
    with pytest.raises(ValidationError):
        policy.full_clean()

    policy.min_supported_version = 'garbage'
    with pytest.raises(ValidationError):
        policy.full_clean()


# --- accès public -----------------------------------------------------------


def test_public_even_with_invalid_or_unvalidated_token(policy, make_user, api):
    bad_token = APIClient()
    bad_token.credentials(HTTP_AUTHORIZATION='Bearer not-a-jwt')
    assert _check(bad_token, platform='ios', version='1.0.7').status_code == 200

    pending = make_user('pending', validated=False)
    assert _check(api.as_user(pending), platform='ios', version='1.0.7').status_code == 200


# --- middleware 426 ---------------------------------------------------------


def _with_headers(version, platform='ios'):
    client = APIClient()
    client.credentials(HTTP_X_APP_VERSION=version, HTTP_X_APP_PLATFORM=platform)
    return client


def test_enforcement_off_by_default(policy):
    assert _with_headers('1.0.0').get(PROTECTED_URL).status_code == 401


def test_enforcement_blocks_outdated_clients(policy, settings):
    settings.APP_VERSION_ENFORCEMENT = True

    blocked = _with_headers('1.0.7').get(PROTECTED_URL)
    assert blocked.status_code == 426
    assert blocked.json()['status'] == 'update_required'
    assert blocked.json()['store_url'] == 'https://apps.apple.com/app/id1'

    # Version supportée, en-têtes absents ou illisibles → la requête continue (401 = pas de JWT).
    assert _with_headers('1.0.8').get(PROTECTED_URL).status_code == 401
    assert APIClient().get(PROTECTED_URL).status_code == 401
    assert _with_headers('junk').get(PROTECTED_URL).status_code == 401
    assert _with_headers('1.0.0', platform='web').get(PROTECTED_URL).status_code == 401


@pytest.mark.parametrize(
    'path, method',
    [(URL + '?platform=ios&version=1.0.7', 'get'), ('/api/health/', 'get'), ('/api/auth/token/refresh/', 'post')],
)
def test_enforcement_exemptions(policy, settings, path, method):
    settings.APP_VERSION_ENFORCEMENT = True

    resp = getattr(_with_headers('1.0.7'), method)(path)

    assert resp.status_code != 426
