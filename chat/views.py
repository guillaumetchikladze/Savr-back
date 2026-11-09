from django.utils import timezone
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsValidated as IsAuthenticated
from chat.models import Conversation, Message, MessageFeedback
from chat.serializers import ChatSessionSerializer, ConversationSerializer, MessageSerializer
from chat.services.conversation_title import clean_user_title
from chat.services.history import sessions_queryset


class ConversationListCreateView(generics.ListCreateAPIView):
    """Contrat historique (app 1.0.9) : toutes les conversations non supprimées, vides comprises."""

    permission_classes = [IsAuthenticated]
    serializer_class = ConversationSerializer

    def get_queryset(self):
        return sessions_queryset(self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ChatSessionListCreateView(ConversationListCreateView):
    """
    « Tes conversations » : `GET /api/chat/sessions/` (paginé `page` / `page_size`, plus récentes d'abord,
    conversations sans message utilisateur exclues). `POST` crée une conversation vide (absente de la liste
    tant qu'aucun message n'est envoyé).
    """

    serializer_class = ChatSessionSerializer

    def get_queryset(self):
        if self.request.method == 'GET':
            return sessions_queryset(self.request.user, only_with_messages=True)
        return Conversation.objects.none()

    def create(self, request, *args, **kwargs):
        conv = Conversation.objects.create(user=request.user)
        conv = sessions_queryset(request.user).get(pk=conv.pk)
        return Response(ChatSessionSerializer(conv).data, status=status.HTTP_201_CREATED)


class ChatSessionDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    `GET|PATCH|DELETE /api/chat/sessions/<id>/` (et `conversations/<id>/`). Conversation d'un autre ou
    supprimée → 404. `PATCH {"title": "…"}` renomme (le titre auto ne la réécrit plus). `DELETE` = suppression
    douce (`deleted_at`) : masquée partout tout de suite (listes, messages, WebSocket), purgée définitivement
    après `CHAT_DELETED_RETENTION_DAYS` (beat `chat-purge-deleted-conversations`).
    """

    permission_classes = [IsAuthenticated]
    serializer_class = ChatSessionSerializer
    http_method_names = ['get', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return sessions_queryset(self.request.user)

    def update(self, request, *args, **kwargs):
        conv = self.get_object()
        try:
            title = clean_user_title(request.data.get('title'))
        except ValueError as exc:
            return Response({'code': 'invalid_title', 'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        # .update() : renommer ne fait pas remonter la conversation dans la liste (updated_at inchangé).
        Conversation.objects.filter(pk=conv.pk).update(title=title, title_source=Conversation.TITLE_USER)
        conv = self.get_queryset().get(pk=conv.pk)
        return Response(self.get_serializer(conv).data)

    def destroy(self, request, *args, **kwargs):
        conv = self.get_object()
        Conversation.objects.filter(pk=conv.pk, deleted_at__isnull=True).update(deleted_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConversationDetailView(ChatSessionDetailView):
    """Contrat historique : mêmes actions, payload `ConversationSerializer` (champs 1.0.9)."""

    serializer_class = ConversationSerializer


def _int_param(params, name, default, *, minimum=0, maximum=None):
    try:
        value = int(params.get(name, default))
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    return min(value, maximum) if maximum is not None else value


class MessageListView(generics.ListAPIView):
    """
    Messages d'une conversation, ordre chronologique. Pagination historique `offset` / `page_size` (≤ 100).
    Reprise : `?latest=1` → les `page_size` derniers messages ; `?before=<message_id>` → les `page_size`
    messages juste avant (charger plus ancien). `has_more` : il reste des messages plus anciens (latest/before)
    ou plus récents (offset).
    """

    permission_classes = [IsAuthenticated]
    serializer_class = MessageSerializer

    def get_queryset(self):
        conversation_id = self.kwargs['conversation_id']
        conv = Conversation.objects.alive().filter(id=conversation_id, user=self.request.user).first()
        if not conv:
            return Message.objects.none()
        return Message.objects.filter(conversation=conv).order_by('created_at', 'id')

    def list(self, request, *args, **kwargs):
        conversation_id = kwargs['conversation_id']
        if not Conversation.objects.alive().filter(id=conversation_id, user=request.user).exists():
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        params = request.query_params
        queryset = self.get_queryset()
        page_size = _int_param(params, 'page_size', 50, minimum=1, maximum=100)
        before = _int_param(params, 'before', 0)
        from_end = bool(before) or str(params.get('latest', '')).lower() in ('1', 'true', 'yes')
        if before:
            queryset = queryset.filter(id__lt=before)
        total = queryset.count()
        if from_end:
            offset = max(total - page_size, 0)
        else:
            offset = _int_param(params, 'offset', 0)
        items = list(queryset[offset : offset + page_size])
        has_more = offset > 0 if from_end else offset + len(items) < total
        feedback_map = {
            fb.message_id: fb
            for fb in MessageFeedback.objects.filter(
                message__in=[m.id for m in items],
                user=request.user,
            )
        }
        serializer = self.get_serializer(
            items,
            many=True,
            context={**self.get_serializer_context(), 'user_feedback_map': feedback_map},
        )
        return Response(
            {
                'count': total,
                'offset': offset,
                'page_size': page_size,
                'has_more': has_more,
                'results': serializer.data,
            }
        )


class MessageFeedbackView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_message(self, request, message_id):
        return (
            Message.objects.select_related('conversation')
            .filter(id=message_id, conversation__user=request.user, conversation__deleted_at__isnull=True)
            .first()
        )

    def post(self, request, message_id):
        message = self._get_message(request, message_id)
        if not message:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        rating = (request.data.get('rating') or '').strip().lower()
        if rating not in {MessageFeedback.RATING_UP, MessageFeedback.RATING_DOWN}:
            return Response(
                {'detail': 'rating must be "up" or "down".'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing = MessageFeedback.objects.filter(message=message, user=request.user).first()
        if existing and existing.rating == rating:
            existing.delete()
            return Response({'rating': None})

        feedback, _ = MessageFeedback.objects.update_or_create(
            message=message,
            user=request.user,
            defaults={'rating': rating},
        )
        return Response(
            {
                'rating': feedback.rating,
                'created_at': feedback.created_at.isoformat(),
                'updated_at': feedback.updated_at.isoformat(),
            }
        )
