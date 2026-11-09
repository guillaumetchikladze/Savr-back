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
def send_friends_digests_task() -> dict:
    from .retention import send_friends_digests

    report = send_friends_digests()
    if report:
        logger.info('[notify] digest amis : %s', report)
    return report


@shared_task(ignore_result=True)
def send_weekly_recaps_task() -> dict:
    from .retention import send_weekly_recaps

    report = send_weekly_recaps()
    if report:
        logger.info('[notify] récap hebdo : %s', report)
    return report
