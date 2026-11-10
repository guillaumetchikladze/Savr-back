"""Connexion WebSocket du chat : jeton valide, compte validé ET actif."""

from unittest import mock

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator

from chat.consumers import ChatConsumer
from core.ws_auth import get_active_user


def _connect(user):
    """Connexion avec `get_user_from_token` simulé (la recherche en base est testée à part)."""

    async def run():
        communicator = WebsocketCommunicator(ChatConsumer.as_asgi(), '/ws/chat/?token=x')
        connected, code = await communicator.connect()
        if connected:
            await communicator.disconnect()
        return connected, code

    with mock.patch('chat.consumers.get_user_from_token', mock.AsyncMock(return_value=user)):
        return async_to_sync(run)()


def test_active_validated_user_can_connect(user):
    assert _connect(user)[0]


def test_inactive_user_is_refused_by_the_consumer(user):
    user.is_active = False
    assert _connect(user) == (False, 4003)


def test_unvalidated_user_and_unknown_token_are_still_refused(make_user):
    assert _connect(make_user('attente', validated=False)) == (False, 4003)
    assert _connect(None) == (False, 4003)


def test_token_lookup_ignores_deactivated_accounts(user):
    assert get_active_user(user.pk) == user
    user.is_active = False
    user.save(update_fields=['is_active'])
    assert get_active_user(user.pk) is None
