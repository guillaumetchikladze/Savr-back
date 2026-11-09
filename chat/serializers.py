from rest_framework import serializers

from chat.models import Conversation, Message, MessageFeedback, PendingAction
from chat.services.history import message_preview


class ConversationSerializer(serializers.ModelSerializer):
    """Contrat historique (`/api/chat/conversations/`, app 1.0.9) : champs inchangés."""

    last_message_preview = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ['id', 'title', 'created_at', 'updated_at', 'last_message_preview']
        read_only_fields = ['id', 'created_at', 'updated_at', 'last_message_preview']

    def get_last_message_preview(self, obj):
        if hasattr(obj, 'last_message_role'):  # annoté par history.sessions_queryset (0 requête)
            return message_preview(obj.last_message_role, obj.last_message_content, obj.last_message_ui_payload)
        msg = Message.objects.filter(conversation=obj).order_by('-created_at', '-id').first()
        if not msg:
            return ''
        return message_preview(msg.role, msg.content, msg.ui_payload)


class ChatSessionSerializer(ConversationSerializer):
    """Historique « Tes conversations » (`/api/chat/sessions/`) : annotations de `sessions_queryset`."""

    title_source = serializers.CharField(read_only=True)
    last_message_role = serializers.CharField(read_only=True, allow_null=True, default=None)
    last_message_at = serializers.DateTimeField(read_only=True, allow_null=True, default=None)
    message_count = serializers.IntegerField(read_only=True, default=0)

    class Meta(ConversationSerializer.Meta):
        fields = [
            'id',
            'title',
            'title_source',
            'created_at',
            'updated_at',
            'last_message_at',
            'last_message_role',
            'last_message_preview',
            'message_count',
        ]
        read_only_fields = fields


class MessageFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessageFeedback
        fields = ['rating', 'created_at', 'updated_at']
        read_only_fields = fields


class MessageSerializer(serializers.ModelSerializer):
    pending_action = serializers.SerializerMethodField()
    user_feedback = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id',
            'role',
            'message_type',
            'content',
            'ui_payload',
            'turn_id',
            'created_at',
            'pending_action',
            'user_feedback',
        ]
        read_only_fields = fields

    def get_user_feedback(self, obj):
        feedback_map = self.context.get('user_feedback_map') or {}
        feedback = feedback_map.get(obj.id)
        if not feedback:
            return None
        return MessageFeedbackSerializer(feedback).data

    def get_pending_action(self, obj):
        if obj.message_type != Message.TYPE_MUTATION_PROPOSAL:
            return None
        action = PendingAction.objects.filter(message=obj, status=PendingAction.STATUS_PENDING).first()
        if not action:
            return None
        action.mark_expired_if_needed()
        if action.status != PendingAction.STATUS_PENDING:
            return None
        return {
            'action_id': str(action.id),
            'action_type': action.action_type,
            'status': action.status,
            'expires_at': action.expires_at.isoformat(),
        }
