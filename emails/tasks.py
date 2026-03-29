from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from savr_back.celery import app

from .graph_client import GraphEmailError, send_mail_via_graph
from .models import EmailQueue, EmailStatus
from .services import priority_to_celery_queue, render_email_from_queue


def _retry_countdown(retries: int) -> int:
    # Backoff simple: 30s, 2m, 5m, 10m...
    schedule = [30, 120, 300, 600, 900]
    idx = min(retries, len(schedule) - 1)
    return schedule[idx]


@app.task(bind=True, max_retries=0, acks_late=True)
def send_queued_email(self, email_id: int) -> str:
    """
    Envoie 1 email de la table EmailQueue.
    - verrouille la ligne (évite double envoi)
    - met à jour status/retries/error
    - re-queue avec backoff si retries < max_retries
    """
    with transaction.atomic():
        row = (
            EmailQueue.objects.select_for_update()
            .filter(pk=email_id)
            .first()
        )
        if not row:
            return "missing"
        if row.status not in (EmailStatus.PENDING, EmailStatus.FAILED):
            return f"skip:{row.status}"
        if row.retries >= row.max_retries:
            row.status = EmailStatus.FAILED
            row.error = row.error or "Max retries reached."
            row.save(update_fields=["status", "error", "updated_at"])
            return "max_retries"

        row.status = EmailStatus.PROCESSING
        row.error = None
        row.save(update_fields=["status", "error", "updated_at"])

    try:
        rendered = render_email_from_queue(row)
        send_mail_via_graph(
            from_email=row.from_email,
            to_email=row.to_email,
            subject=row.subject,
            html=rendered.html,
            text=rendered.text,
        )
    except Exception as e:
        err = str(e)
        with transaction.atomic():
            row = EmailQueue.objects.select_for_update().get(pk=email_id)
            row.retries = row.retries + 1
            row.status = EmailStatus.FAILED
            row.error = err[:4000]
            row.save(update_fields=["retries", "status", "error", "updated_at"])

            should_retry = row.retries < row.max_retries and isinstance(e, GraphEmailError)

        if should_retry:
            countdown = _retry_countdown(row.retries)
            send_queued_email.apply_async(
                args=[email_id],
                countdown=countdown,
                queue=priority_to_celery_queue(row.priority),
            )
            return f"retry_in:{countdown}"
        return "failed"

    with transaction.atomic():
        row = EmailQueue.objects.select_for_update().get(pk=email_id)
        row.status = EmailStatus.SENT
        row.sent_at = timezone.now()
        row.error = None
        row.save(update_fields=["status", "sent_at", "error", "updated_at"])
    return "sent"

