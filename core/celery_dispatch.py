"""
Envoi de tâches Celery depuis une requête HTTP : après commit, sans jamais bloquer ni lever.

- `transaction.on_commit` : la tâche ne part qu'une fois les lignes qu'elle lit visibles par le worker
  (et pas du tout si la transaction est annulée). Hors transaction : envoi immédiat.
- Publication bornée (`PUBLISH_RETRY_POLICY`) : broker indisponible → avertissement en log, la requête
  continue (≈ 0 s au lieu de plusieurs secondes de reconnexion).
- Les tâches envoyées ainsi doivent être `ignore_result=True` : sinon `apply_async` s'abonne d'abord au
  backend de résultats Redis, et un Redis absent coûte ~19 s de reconnexion dans la requête.
"""

from __future__ import annotations

import logging

from django.db import transaction

logger = logging.getLogger(__name__)

PUBLISH_RETRY_POLICY = {'max_retries': 1, 'interval_start': 0, 'interval_step': 0.2, 'interval_max': 0.2}


def publish(task, *, args=(), kwargs=None, **options) -> bool:
    """`task.apply_async` sans exception (False si le broker a refusé)."""
    try:
        task.apply_async(args=list(args), kwargs=kwargs or {}, retry_policy=PUBLISH_RETRY_POLICY, **options)
        return True
    except Exception:
        logger.warning('[celery] publication de %s impossible (broker indisponible ?)', task.name, exc_info=True)
        return False


def enqueue_on_commit(task, *, args=(), kwargs=None, **options) -> None:
    """Planifie `publish(task, …)` au commit de la transaction courante."""
    transaction.on_commit(lambda: publish(task, args=args, kwargs=kwargs, **options))
