"""Tâches Celery des notifications : push groupées (debounce) et rétention (beat, toutes les heures)."""

from __future__ import annotations

import logging

from celery import shared_task

from . import grouping

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=15, ignore_result=True)
def send_grouped_push_task(self, user_id: int, type_key: str, object_id: int, version=None) -> str:
    """Push agrégée d'un groupe (ex. miams d'un post) ; no-op si une version plus récente est planifiée."""
    from .services import send_grouped_push

    if not grouping.is_latest_version(user_id=user_id, type_key=type_key, object_id=object_id, version=version):
        return 'superseded'
    result = send_grouped_push(user_id, type_key, object_id)
    grouping.clear(user_id=user_id, type_key=type_key, object_id=object_id)
    return result.status


@shared_task(ignore_result=True)
def send_push_task(
    type_key: str,
    user_id: int,
    data: dict | None = None,
    title: str | None = None,
    body: str | None = None,
    inbox_id: int | None = None,
    dedupe_key: str = '',
) -> str:
    """Push différée par `notify(..., defer_push=True)` (hors requête HTTP) ; `QUIET_DEFER` → replanifiée."""
    from django.contrib.auth import get_user_model

    from accounts.models import Notification
    from core.celery_dispatch import publish

    from .services import push

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return 'missing'
    inbox = Notification.objects.filter(pk=inbox_id, user_id=user_id).first() if inbox_id else None
    result = push(type_key, user, data=data, title=title, body=body, inbox=inbox, dedupe_key=dedupe_key)
    if result.status == 'deferred' and result.retry_at is not None:
        publish(
            send_push_task,
            args=[type_key, user_id],
            kwargs={'data': data, 'title': title, 'body': body, 'inbox_id': inbox_id, 'dedupe_key': dedupe_key},
            eta=result.retry_at,
        )
    return result.status


@shared_task(ignore_result=True)
def send_friends_digests_task() -> dict:
    from .retention import send_friends_digests

    report = send_friends_digests()
    if report:
        logger.info('[notify] digest amis : %s', report)
    return report


@shared_task(ignore_result=True)
def send_weekly_recaps_task() -> dict:
    """Beat du dimanche : récap hebdo + push des tablées (« 3 sur 5 ont cuisiné dans « Les colocs » »)."""
    from .retention import send_weekly_recaps_and_tables

    report = send_weekly_recaps_and_tables()
    if report:
        logger.info('[notify] récap hebdo + tablées : %s', report)
    return report


@shared_task(ignore_result=True)
def send_cooked_pending_reminders_task() -> dict:
    from .retention import send_cooked_pending_reminders

    report = send_cooked_pending_reminders()
    if report:
        logger.info('[notify] rappels « Je l’ai déjà » : %s', report)
    return report
