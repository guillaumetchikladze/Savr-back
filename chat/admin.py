from django.contrib import admin

from chat.models import Conversation, Message, PendingAction


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ['id', 'title', 'user', 'updated_at']
    list_filter = ['updated_at']
    search_fields = ['title', 'user__username']
    raw_id_fields = ['user']


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'conversation', 'role', 'message_type', 'created_at']
    list_filter = ['role', 'message_type']
    raw_id_fields = ['conversation']


@admin.register(PendingAction)
class PendingActionAdmin(admin.ModelAdmin):
    list_display = ['id', 'action_type', 'status', 'conversation', 'expires_at', 'created_at']
    list_filter = ['action_type', 'status']
    raw_id_fields = ['conversation', 'message']
