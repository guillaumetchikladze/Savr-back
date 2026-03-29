from __future__ import annotations

from dataclasses import dataclass

from django.template.loader import render_to_string

from .models import EmailPriority, EmailQueue


@dataclass(frozen=True)
class RenderedEmail:
    html: str
    text: str


def render_email_from_queue(row: EmailQueue) -> RenderedEmail:
    content = row.content or {}

    html = (content.get("html") or "").strip()
    text = (content.get("text") or "").strip()
    template_name = (content.get("template_name") or content.get("template") or "").strip()
    context = content.get("context") or content.get("props") or {}

    if (html or text) and not template_name:
        return RenderedEmail(html=html, text=text)

    if not template_name:
        raise ValueError("EmailQueue.content must include template_name or html/text.")

    # Autoriser "emails/welcome" ou "emails/welcome.html"
    base = template_name[:-5] if template_name.endswith(".html") else template_name
    html_name = f"{base}.html"
    txt_name = f"{base}.txt"

    rendered_html = render_to_string(html_name, context)
    rendered_text = render_to_string(txt_name, context)
    return RenderedEmail(html=rendered_html, text=rendered_text)


def priority_to_celery_queue(priority: str) -> str:
    p = (priority or EmailPriority.NORMAL).upper()
    if p == EmailPriority.URGENT:
        return "emails_urgent"
    if p == EmailPriority.HIGH:
        return "emails_high"
    if p == EmailPriority.LOW:
        return "emails_low"
    return "emails_normal"


def enqueue_email(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    content: dict,
    action_name: str | None = None,
    priority: str = EmailPriority.NORMAL,
    user_id: int | None = None,
    max_retries: int = 3,
) -> EmailQueue:
    row = EmailQueue.objects.create(
        action_name=action_name,
        priority=priority,
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        content=content or {},
        user_id=user_id,
        max_retries=max_retries,
    )

    # Import local pour éviter les imports circulaires au démarrage.
    from .tasks import send_queued_email

    send_queued_email.apply_async(
        args=[row.id],
        queue=priority_to_celery_queue(priority),
    )
    return row

