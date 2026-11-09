"""
Historique des conversations (« Tes conversations ») : requêtes à nombre constant, aperçu du dernier message.

- `sessions_queryset(user)` : conversations **non supprimées** de l'utilisateur, annotées (dernier message :
  rôle, contenu, payload, date ; nombre de messages utilisateur + assistant ; présence d'un message utilisateur)
  par sous-requêtes → 1 requête pour toute une page, quel que soit le nombre de conversations.
- `message_preview(role, content, ui_payload)` : aperçu ≤ 120 caractères (JSON d'outils retiré).
"""

from __future__ import annotations

from django.db.models import Count, Exists, IntegerField, JSONField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce

from chat.models import Conversation, Message
from chat.services.text_sanitize import strip_tool_json_segments

PREVIEW_CHARS = 120
COUNTED_ROLES = (Message.ROLE_USER, Message.ROLE_ASSISTANT)


def message_preview(role: str | None, content: str | None, ui_payload) -> str:
    if role is None:
        return ''
    text = ' '.join((content or '').split())
    if role == Message.ROLE_USER:
        return text[:PREVIEW_CHARS]
    clean = ' '.join(strip_tool_json_segments(content or '').split())
    if clean:
        return clean[:PREVIEW_CHARS]
    traces = (ui_payload or {}).get('tool_traces') if isinstance(ui_payload, dict) else None
    if traces:
        return 'Tchikook a consulté tes données'
    return ''


def sessions_queryset(user, *, only_with_messages: bool = False):
    last = Message.objects.filter(conversation=OuterRef('pk')).order_by('-created_at', '-id')
    counted = (
        Message.objects.filter(conversation=OuterRef('pk'), role__in=COUNTED_ROLES)
        .order_by()
        .values('conversation')
        .annotate(c=Count('id'))
        .values('c')
    )
    qs = (
        Conversation.objects.alive()
        .filter(user=user)
        .annotate(
            last_message_role=Subquery(last.values('role')[:1]),
            last_message_content=Subquery(last.values('content')[:1]),
            last_message_ui_payload=Subquery(last.values('ui_payload')[:1], output_field=JSONField()),
            last_message_at=Subquery(last.values('created_at')[:1]),
            message_count=Coalesce(Subquery(counted, output_field=IntegerField()), Value(0)),
            has_user_message=Exists(Message.objects.filter(Q(conversation=OuterRef('pk')) & Q(role=Message.ROLE_USER))),
        )
        .order_by('-updated_at', '-id')
    )
    if only_with_messages:
        qs = qs.filter(has_user_message=True)
    return qs
