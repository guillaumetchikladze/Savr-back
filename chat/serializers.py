from rest_framework import serializers

from chat.models import Conversation, Message, MessageFeedback, PendingAction
from chat.services.text_sanitize import strip_tool_json_segments


class ConversationSerializer(serializers.ModelSerializer):
    last_message_preview = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ['id', 'title', 'created_at', 'updated_at', 'last_message_preview']
        read_only_fields = ['id', 'created_at', 'updated_at', 'last_message_preview']

    def get_last_message_preview(self, obj):
        msg = (
            Message.objects.filter(conversation=obj)
            .order_by('-created_at')
            .first()
        )
        if not msg:
            return ''
        if msg.role == Message.ROLE_USER:
            return (msg.content or '')[:120]
        clean = strip_tool_json_segments(msg.content or '')
        if clean:
            return clean[:120]
        traces = (msg.ui_payload or {}).get('tool_traces') or []
        if traces:
            return 'Tchikook Agent a consulté vos données'
        return ''


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
        action = (
            PendingAction.objects.filter(message=obj, status=PendingAction.STATUS_PENDING)
            .first()
        )
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
