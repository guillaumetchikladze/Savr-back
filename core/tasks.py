"""Tâches Celery du socle : purge des réponses idempotentes expirées (beat, toutes les heures)."""

from __future__ import annotations

from celery import shared_task


@shared_task(ignore_result=True)
def purge_expired_idempotency_records_task() -> int:
    from .idempotency import purge_expired

    return purge_expired()
