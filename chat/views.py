from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from chat.models import Conversation, Message
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
        serializer = self.get_serializer(items, many=True)
        return Response({
            'count': total,
            'offset': offset,
            'page_size': page_size,
            'results': serializer.data,
        })
