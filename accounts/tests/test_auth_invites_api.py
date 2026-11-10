"""Liens d'invitation : inscription avec code (liste d'attente, amitié mutuelle, notif), codes invalides / limites."""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import AllowedEmail, Follow, InviteRedemption, Notification, UserBlock

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    cache.clear()
    yield
    cache.clear()


def _register(code, email='bob@example.test', username='bob'):
    return APIClient().post(
        '/api/auth/register/',
        {'email': email, 'username': username, 'password': 'Sup3r-secret!', 'invite_code': code},
        format='json',
    )


def _mutual(a, b):
    return (
        Follow.objects.filter(follower=a, following=b).exists()
        and Follow.objects.filter(follower=b, following=a).exists()
    )


def test_signup_with_invite_skips_waitlist_befriends_and_notifies_inviter(api, user, settings):
    settings.INVITE_BASE_URL = 'https://tchikook.fr'
    me = api.as_user(user).get('/api/invites/me/')
    assert me.status_code == 200
    code = me.data['code']
    assert len(code) == 10 and me.data['url'] == f'https://tchikook.fr/i/{code}' and me.data['uses'] == 0
    assert api.get('/api/invites/me/').data['code'] == code  # stable

    preview = APIClient().get(f'/api/invites/{code.upper()}/')  # public, insensible à la casse
    assert preview.status_code == 200 and preview.data['inviter']['username'] == 'alice'
    assert 'email' not in preview.data['inviter']

    resp = _register(f' {code.upper()} ')
    assert resp.status_code == 201, resp.data
    assert resp.data['user']['is_validated'] is True
    assert resp.data['invite']['status'] == 'redeemed' and resp.data['invite']['bypassed_waitlist'] is True
    assert resp.data['invite']['inviter']['display_name'] == 'alice'  # sans prénom : pseudo
    bob_id = resp.data['user']['id']
    assert AllowedEmail.objects.filter(email='bob@example.test').exists()
    assert _mutual(user, get_user_model().objects.get(id=bob_id))

    redemption = InviteRedemption.objects.get(inviter=user, invitee_id=bob_id)
    assert redemption.at_signup and redemption.is_acquisition and redemption.bypassed_waitlist
    notif = Notification.objects.get(user=user, related_user_id=bob_id)
    assert notif.notification_type == 'follow'  # lu par l'app 1.0.9 comme un nouvel ami
    assert (notif.title, notif.message) == ('Un ami de plus', 'bob a rejoint Tchikook grâce à toi.')
    assert api.get('/api/invites/me/').data['uses'] == 1


def test_invalid_own_exhausted_rotated_and_rate_limited_codes(api, user, make_user, settings):
    code = api.as_user(user).get('/api/invites/me/').data['code']

    # Code invalide : l'inscription passe quand même, sans sortir de la liste d'attente.
    resp = _register('nope-nope-nope')
    assert resp.status_code == 201
    assert resp.data['user']['is_validated'] is False and resp.data['invite']['code'] == 'invalid_code'

    assert api.post('/api/invites/redeem/', {'code': code}, format='json').data['code'] == 'own_code'

    # Compte existant en attente + réglage « pas de passe-droit » : amis, mais toujours en attente.
    settings.INVITE_BYPASSES_WAITLIST = False
    user.first_name = 'Alice'
    user.save(update_fields=['first_name'])
    waiting = make_user('waiting', validated=False)
    resp = api.as_user(waiting).post('/api/invites/redeem/', {'code': f'https://tchikook.fr/i/{code}'}, format='json')
    assert resp.status_code == 201 and resp.data['user']['is_validated'] is False
    assert _mutual(user, waiting) and resp.data['invite']['bypassed_waitlist'] is False
    assert resp.data['invite']['inviter']['display_name'] == 'Alice'  # prénom d'abord
    again = api.post('/api/invites/redeem/', {'code': code}, format='json')
    assert again.status_code == 200 and again.data['invite']['status'] == 'already_redeemed'

    # Limite d'utilisations par code.
    settings.INVITE_CODE_MAX_USES = 1
    other = make_user('other')
    assert api.as_user(other).post('/api/invites/redeem/', {'code': code}, format='json').status_code == 410

    # Blocage : le lien ne marche pas (sans rien révéler).
    blocked = make_user('blocked')
    UserBlock.objects.create(blocker=user, blocked=blocked)
    settings.INVITE_CODE_MAX_USES = 0
    resp = api.as_user(blocked).post('/api/invites/redeem/', {'code': code}, format='json')
    assert resp.status_code == 404 and not Follow.objects.filter(follower=blocked).exists()

    # Nouveau lien : l'ancien est mort.
    rotated = api.as_user(user).post('/api/invites/me/rotate/')
    assert rotated.status_code == 201 and rotated.data['code'] != code
    assert APIClient().get(f'/api/invites/{code}/').status_code == 404

    with mock.patch('accounts.views_invites.LOOKUP_LIMIT_PER_IP', 2):
        statuses = [APIClient().get(f'/api/invites/{rotated.data["code"]}/').status_code for _ in range(3)]
    assert statuses[-1] == 429


def test_redeem_locks_the_code_row_before_counting_uses(user, make_user, settings):
    """Course sur `max_uses` : le code est verrouillé (SELECT … FOR UPDATE) avant le comptage des utilisations."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from accounts.models import InviteCode
    from accounts.services.invite_service import InviteError, get_or_create_active_code, redeem, resolve_code

    settings.INVITE_CODE_MAX_USES = 2
    code = get_or_create_active_code(user).code
    with CaptureQueriesContext(connection) as ctx:
        assert redeem(make_user('a'), code).created
    sql = [q['sql'] for q in ctx.captured_queries]
    lock = next(i for i, q in enumerate(sql) if 'FOR UPDATE' in q and 'accounts_invitecode' in q)
    count = next(i for i, q in enumerate(sql) if 'COUNT(' in q and 'accounts_inviteredemption' in q)
    assert lock < count

    assert redeem(make_user('b'), code).created
    with pytest.raises(InviteError) as exc:
        redeem(make_user('c'), code)
    assert exc.value.code == 'exhausted'

    # Code régénéré entre la résolution et le verrou : refusé, rien n'est créé.
    stale = resolve_code(code)
    InviteCode.objects.filter(pk=stale.pk).update(is_active=False)
    late = make_user('late', validated=False)
    settings.INVITE_CODE_MAX_USES = 0
    with mock.patch('accounts.services.invite_service.resolve_code', return_value=stale):
        with pytest.raises(InviteError) as exc:
            redeem(late, code)
    assert exc.value.code == 'invalid_code'
    late.refresh_from_db()
    assert late.validated_at is None and not late.invites_received.exists()
