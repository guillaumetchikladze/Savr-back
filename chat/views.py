from rest_framework import generics, status
from accounts.permissions import IsValidated as IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from chat.models import Conversation, Message, MessageFeedback
from chat.serializers import ConversationSerializer, MessageSerializer


class ConversationListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ConversationSerializer

    def get_queryset(self):
        return Conversation.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class ConversationDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ConversationSerializer

    def get_queryset(self):
        return Conversation.objects.filter(user=self.request.user)


class MessageListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = MessageSerializer

    def get_queryset(self):
        conversation_id = self.kwargs['conversation_id']
        conv = Conversation.objects.filter(
            id=conversation_id, user=self.request.user
        ).first()
        if not conv:
            return Message.objects.none()
        return Message.objects.filter(conversation=conv).order_by('created_at')

    def list(self, request, *args, **kwargs):
        conversation_id = kwargs['conversation_id']
        if not Conversation.objects.filter(id=conversation_id, user=request.user).exists():
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        queryset = self.get_queryset()
        page_size = min(int(request.query_params.get('page_size', 50)), 100)
        offset = max(int(request.query_params.get('offset', 0)), 0)
        total = queryset.count()
        items = queryset[offset:offset + page_size]
        feedback_map = {
            fb.message_id: fb
            for fb in MessageFeedback.objects.filter(
                message__in=items,
                user=request.user,
            )
        }
        serializer = self.get_serializer(
            items,
            many=True,
            context={**self.get_serializer_context(), 'user_feedback_map': feedback_map},
        )
        return Response({
            'count': total,
            'offset': offset,
            'page_size': page_size,
            'results': serializer.data,
        })


class MessageFeedbackView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_message(self, request, message_id):
        return (
            Message.objects.select_related('conversation')
            .filter(id=message_id, conversation__user=request.user)
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
        return Response({
            'rating': feedback.rating,
            'created_at': feedback.created_at.isoformat(),
            'updated_at': feedback.updated_at.isoformat(),
        })
