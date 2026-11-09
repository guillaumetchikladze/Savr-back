from datetime import date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

PARIS = ZoneInfo('Europe/Paris')


@pytest.fixture
def expo():
    """Capture les envois Expo (aucun appel réseau) : `expo.messages` = toutes les push envoyées."""
    with mock.patch('notifications.services.send_expo_push_notifications') as send:
        send.messages = []
        send.side_effect = lambda msgs: send.messages.extend(list(msgs))
        yield send


@pytest.fixture
def device(db):
    from accounts.models import PushDevice

    def _device(user, token: str | None = None):
        return PushDevice.objects.create(
            user=user, expo_push_token=token or f'ExponentPushToken[{user.id}]', platform='ios'
        )

    return _device


@pytest.fixture
def paris():
    """`paris(18, 0)` → datetime aware, aujourd'hui (ou `day`) à 18:00 heure de Paris."""

    def _at(hour: int, minute: int = 0, day: date | None = None) -> datetime:
        d = day or datetime.now(PARIS).date()
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=PARIS)

    return _at


@pytest.fixture
def make_post(db):
    from recipes.models import Post

    def _post(author, label: str = 'Quiche', *, at: datetime | None = None, published: bool = True):
        post = Post.objects.create(user=author, label=label, is_published=published)
        if at is not None:
            Post.objects.filter(pk=post.pk).update(created_at=at)
            post.refresh_from_db()
        return post

    return _post
