"""Endpoints : préférences par catégorie (GET/PATCH) et suivi d'ouverture des push."""

from accounts.models import Notification
from notifications.models import NotificationDelivery, NotificationPreference
from notifications.services import notify

PREFS = '/api/notifications/preferences/'
OPENED = '/api/notifications/opened/'


def test_preferences_defaults_and_patch(api, user):
    client = api.as_user(user)
    body = client.get(PREFS).json()
    keys = [c['key'] for c in body['categories']]
    assert keys == ['social', 'friends_activity', 'weekly_recap', 'rewards', 'cooking_reminders', 'planning']
    assert all(c['enabled'] for c in body['categories'])
    assert body['quiet_hours'] == {'start': '22:00', 'end': '08:00'}
    assert body['timezone'] == 'Europe/Paris' and body['daily_cap'] == 2
    assert not NotificationPreference.objects.exists()  # pas de ligne tant qu'on ne modifie rien

    resp = client.patch(
        PREFS,
        {
            'categories': {'friends_activity': False},
            'types': {'follow': False, 'post_miam': True},  # clés de l'ancien écran de réglages
            'quiet_hours': {'start': '21:30', 'end': '07:00'},
            'timezone': 'America/Montreal',
        },
        format='json',
    )
    assert resp.status_code == 200
    data = resp.json()
    assert {c['key']: c['enabled'] for c in data['categories']}['friends_activity'] is False
    assert data['types'] == {'follow_new': False, 'follow_request': False, 'follow_accepted': False}
    assert data['quiet_hours'] == {'start': '21:30', 'end': '07:00'} and data['timezone'] == 'America/Montreal'


def test_preferences_rejects_unknown_or_locked_keys(api, user):
    client = api.as_user(user)
    for payload in (
        {'categories': {'nope': False}},
        {'categories': {'essential': False}},  # minuteurs : non réglable
        {'categories': {'social': 'no'}},
        {'types': {'timer_almost_finished': False}},
        {'quiet_hours': {'start': 'late'}},
        {'timezone': 'Mars/Olympus'},
    ):
        assert client.patch(PREFS, payload, format='json').status_code == 400, payload


def test_opened_tracks_first_open_and_marks_inbox_read(api, user, make_user, device, expo):
    device(user)
    result = notify('follow_request', user, actor=make_user('lea'))
    delivery = result.push.delivery
    assert expo.messages[0]['data']['notification_id'] == delivery.id
    assert expo.messages[0]['data']['inbox_id'] == result.inbox.id

    # Un autre utilisateur ne peut pas marquer cette push.
    assert api.as_user(make_user('eve')).post(OPENED, {'notification_id': delivery.id}).json() == {'tracked': False}

    client = api.as_user(user)
    assert client.post(OPENED, {'notification_id': delivery.id}).json() == {'tracked': True}
    delivery.refresh_from_db()
    first_open = delivery.opened_at
    assert first_open is not None and Notification.objects.get(pk=result.inbox.id).is_read
    client.post(OPENED, {'notification_id': delivery.id})
    delivery.refresh_from_db()
    assert delivery.opened_at == first_open  # première ouverture seulement

    assert client.post(OPENED, {}).status_code == 400
    assert client.post(OPENED, {'type': 'nope'}).status_code == 400


def test_opened_by_type_for_pushes_without_id(api, user, make_user, device, expo):
    device(user)
    notify('follow_request', user, actor=make_user('lea'))
    assert api.as_user(user).post(OPENED, {'type': 'follow_request'}).json() == {'tracked': True}
    assert NotificationDelivery.objects.get().opened_at is not None
    assert api.as_user(user).post(OPENED, {'type': 'follow_request'}).json() == {'tracked': False}
