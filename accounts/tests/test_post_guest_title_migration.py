"""wrd1 : la migration 0032 réécrit le titre des notifs `post_guest` déjà envoyées."""

import importlib

import pytest
from django.apps import apps

from accounts.models import Notification

migration = importlib.import_module('accounts.migrations.0032_post_guest_title_published_with_you')


@pytest.mark.django_db
def test_rewrites_old_post_guest_titles_only(make_user):
    lea = make_user('lea')
    curly = Notification.objects.create(
        user=lea, notification_type='post_guest', title='Tom t’a mis dans son plat', message='m'
    )
    straight = Notification.objects.create(
        user=lea, notification_type='post_guest', title="Tom t'a mis dans son plat", message='m'
    )
    other = Notification.objects.create(
        user=lea, notification_type='miam', title='Tom t’a mis dans son plat', message='m'
    )

    migration.forwards(apps, None)

    curly.refresh_from_db()
    straight.refresh_from_db()
    other.refresh_from_db()
    assert curly.title == straight.title == 'Tom a publié un repas avec toi'
    assert other.title == 'Tom t’a mis dans son plat'
