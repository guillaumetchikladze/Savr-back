"""Règles d'envoi : plafond quotidien, heures calmes, préférences, regroupement des miams."""

from datetime import timedelta

from django.test import override_settings

from accounts.models import Notification
from notifications.models import NotificationDelivery, NotificationPreference
from notifications.services import notify, push, send_grouped_push


@override_settings(NOTIFICATIONS_DAILY_CAP=2)
def test_daily_cap_applies_to_non_transactional_only(user, make_user, device, expo, paris, make_post):
    device(user)
    lea = make_user('lea')
    post = make_post(user)
    noon = paris(12)

    statuses = [
        notify('post_repost', user, actor=lea, post=post, now=noon + timedelta(minutes=i)).push.status for i in range(3)
    ]
    assert statuses == ['sent', 'sent', 'capped']
    # L'in-app reste créée même quand la push est plafonnée.
    assert Notification.objects.filter(user=user, notification_type='post_repost').count() == 3

    # Transactionnel (mention) : hors plafond.
    assert notify('post_comment_mention', user, actor=lea, post=post, now=noon).push.status == 'sent'
    assert len(expo.messages) == 3
    # Le plafond repart le lendemain (jour local).
    assert notify('post_repost', user, actor=lea, post=post, now=noon + timedelta(days=1)).push.status == 'sent'


def test_quiet_hours(user, make_user, device, expo, paris):
    device(user)
    lea = make_user('lea')
    late = paris(23, 30)

    assert notify('follow_new', user, actor=lea, now=late).push.status == 'quiet_hours'
    assert notify('follow_request', user, actor=lea, now=late).push.status == 'sent'  # transactionnel
    timer = push('timer_almost_finished', user, data={'timer_id': 1}, now=late)
    assert timer.status == 'sent'

    deferred = push('cooked_prompt', user, context={'dish_title': 'Quiche'}, data={'meal_plan_id': 1}, now=late)
    assert deferred.status == 'deferred'
    assert deferred.retry_at == paris(8, 0, day=late.date() + timedelta(days=1))
    assert [m['data']['type'] for m in expo.messages] == ['follow_request', 'timer_almost_finished']

    # Heures calmes personnalisées (désactivées si début == fin).
    NotificationPreference.objects.create(user=user, quiet_hours_start=late.time(), quiet_hours_end=late.time())
    assert notify('follow_new', user, actor=lea, now=late).push.status == 'sent'


def test_category_and_type_preferences_mute_push_not_inbox(user, make_user, device, expo, paris, make_post):
    device(user)
    lea = make_user('lea')
    post = make_post(user)
    NotificationPreference.objects.create(user=user, categories={'social': False})

    result = notify('post_repost', user, actor=lea, post=post, now=paris(12))
    assert result.push.status == 'muted' and result.inbox is not None
    # Catégorie non réglable (minuteurs) : toujours envoyée.
    assert push('timer_almost_finished', user, data={'timer_id': 1}, now=paris(12)).status == 'sent'

    NotificationPreference.objects.filter(user=user).update(categories={}, types={'post_repost': False})
    assert notify('post_repost', user, actor=lea, post=post, now=paris(12)).push.status == 'muted'
    assert notify('follow_new', user, actor=lea, now=paris(12)).push.status == 'sent'


def test_miams_are_grouped_into_one_aggregated_push(api, user, make_user, device, expo, make_post, celery_delay):
    device(user)
    post = make_post(user, 'Quiche')
    fans = [make_user(name) for name in ('lea', 'tom', 'zoe', 'max')]
    for fan in fans:
        assert api.as_user(fan).post(f'/api/posts/{post.id}/send_cookie/').status_code == 201

    # Une in-app par miam, aucune push immédiate : des tâches débouncées.
    assert Notification.objects.filter(user=user, notification_type='post_miam').count() == 4
    assert expo.messages == []
    assert {c[0] for c in celery_delay.calls} == {'notifications.tasks.send_grouped_push_task'}

    result = send_grouped_push(user.id, 'post_miam', post.id)
    assert result.status == 'sent'
    (message,) = expo.messages
    assert message['title'] == 'Nouveaux miams'
    assert message['body'] == 'max et 3 autres ont aimé «\u00a0Quiche\u00a0».'
    assert message['collapseId'] == f'post_miam:{post.id}'
    data = message['data']
    assert data['type'] == 'post_miam' and data['category'] == 'social' and data['post_id'] == post.id
    assert data['notification_id'] == NotificationDelivery.objects.get().id
    assert data['url'] == f'tchikook://post/{post.id}'

    # Tout lu dans l'app avant l'envoi : pas de push.
    Notification.objects.filter(user=user).update(is_read=True)
    assert send_grouped_push(user.id, 'post_miam', post.id).status == 'no_unread'
