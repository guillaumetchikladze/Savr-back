"""
Inscription, mot de passe oublié, lien d'invitation : aucun appel réseau dans la requête.
E-mails et push partent en tâche Celery au commit ; sans configuration d'envoi, rien n'est tenté.
"""

from unittest import mock

import pytest
from django.core.cache import cache
from kombu.exceptions import OperationalError
from rest_framework.test import APIClient

from accounts.models import PushDevice
from emails.models import EmailQueue, EmailStatus

pytestmark = pytest.mark.django_db

SEND_EMAIL_TASK = 'emails.tasks.send_queued_email'
SEND_PUSH_TASK = 'notifications.tasks.send_push_task'


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def graph_configured(settings):
    settings.MS_GRAPH_TENANT_ID = 'tenant'
    settings.MS_GRAPH_CLIENT_ID = 'client'
    settings.MS_GRAPH_CLIENT_SECRET = 'secret'
    settings.MS_GRAPH_SENDER = 'noreply@tchikook.fr'


@pytest.fixture
def graph_send():
    """L'envoi réel (MS Graph) : ne doit jamais être appelé pendant la requête."""
    with (
        mock.patch('emails.tasks.send_mail_via_graph') as send,
        mock.patch('emails.graph_client.requests.post') as post,
    ):
        yield send
        post.assert_not_called()


def _register(**extra):
    body = {'email': 'new@example.test', 'username': 'newbie', 'password': 'Sup3r-secret!', **extra}
    return APIClient().post('/api/auth/register/', body, format='json')


def test_register_queues_welcome_email_after_commit(
    graph_configured, graph_send, celery_delay, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks() as callbacks:
        resp = _register()
    assert resp.status_code == 201, resp.data
    row = EmailQueue.objects.get(action_name='welcome')
    assert (row.to_email, row.status, row.priority) == ('new@example.test', EmailStatus.PENDING, 'NORMAL')
    # Rien n'est publié avant le commit, rien n'est envoyé pendant la requête.
    assert celery_delay.calls == [] and len(callbacks) == 1
    graph_send.assert_not_called()

    callbacks[0]()
    assert celery_delay.calls == [(SEND_EMAIL_TASK, (row.id,), {})]
    graph_send.assert_not_called()


def test_password_reset_email_goes_to_high_queue(
    graph_configured, make_user, graph_send, django_capture_on_commit_callbacks
):
    from emails.tasks import send_queued_email

    # Sans ignore_result, apply_async s'abonne au backend Redis (≈ 19 s dans la requête si Redis est absent).
    assert send_queued_email.ignore_result is True
    make_user('bob', email='bob@example.test')
    with (
        mock.patch.object(send_queued_email, 'apply_async') as apply_async,
        django_capture_on_commit_callbacks(execute=True),
    ):
        resp = APIClient().post('/api/auth/password-reset/request/', {'email': 'bob@example.test'}, format='json')
    assert resp.status_code == 200
    row = EmailQueue.objects.get(action_name='password_reset')
    apply_async.assert_called_once()
    assert apply_async.call_args.kwargs['args'] == [row.id]
    assert apply_async.call_args.kwargs['queue'] == 'emails_high'
    graph_send.assert_not_called()


def test_register_without_email_configuration_skips_email(graph_send, celery_delay, django_capture_on_commit_callbacks):
    # settings_test vide les MS_GRAPH_* : ni ligne, ni tâche, ni appel réseau.
    with django_capture_on_commit_callbacks(execute=True):
        resp = _register()
    assert resp.status_code == 201
    assert not EmailQueue.objects.exists()
    assert celery_delay.calls == []
    graph_send.assert_not_called()


def test_register_succeeds_when_broker_is_down(graph_configured, graph_send, django_capture_on_commit_callbacks):
    from celery.app.task import Task

    with (
        mock.patch.object(Task, 'apply_async', side_effect=OperationalError('Connection refused')) as apply_async,
        django_capture_on_commit_callbacks(execute=True),
    ):
        resp = _register()
    assert resp.status_code == 201
    apply_async.assert_called_once()
    # Publication bornée : pas de reconnexion en boucle dans la requête.
    assert apply_async.call_args.kwargs['retry_policy']['max_retries'] <= 1
    assert EmailQueue.objects.get(action_name='welcome').status == EmailStatus.PENDING


def test_signup_with_invite_pushes_inviter_from_celery_not_the_request(
    api, user, celery_delay, django_capture_on_commit_callbacks
):
    PushDevice.objects.create(user=user, expo_push_token='ExponentPushToken[alice]', platform='ios')
    code = api.as_user(user).get('/api/invites/me/').data['code']

    with (
        mock.patch('notifications.services.send_expo_push_notifications') as expo,
        django_capture_on_commit_callbacks(execute=True),
    ):
        resp = _register(invite_code=code)
        assert resp.status_code == 201, resp.data
        assert resp.data['invite']['status'] == 'redeemed'
        expo.assert_not_called()

    push_calls = [c for c in celery_delay.calls if c[0] == SEND_PUSH_TASK]
    assert len(push_calls) == 1
    _, args, kwargs = push_calls[0]
    assert args == ('invite_joined', user.id)

    from notifications.tasks import send_push_task

    with (
        mock.patch('notifications.services.send_expo_push_notifications') as expo,
        mock.patch('notifications.services.in_quiet_hours', return_value=False),
    ):
        assert send_push_task(*args, **kwargs) == 'sent'
    (messages,) = expo.call_args.args
    assert messages[0]['to'] == 'ExponentPushToken[alice]'
    assert messages[0]['data']['user_id'] == resp.data['user']['id']
