from django.urls import path

from chat import views

urlpatterns = [
    path('conversations/', views.ConversationListCreateView.as_view(), name='chat-conversations'),
    path('conversations/<int:pk>/', views.ConversationDetailView.as_view(), name='chat-conversation-detail'),
    path(
        'conversations/<int:conversation_id>/messages/',
        views.MessageListView.as_view(),
        name='chat-messages',
    ),
    # Historique « Tes conversations » (contrat : AGENTS.md § Historique du chat).
    path('sessions/', views.ChatSessionListCreateView.as_view(), name='chat-sessions'),
    path('sessions/<int:pk>/', views.ChatSessionDetailView.as_view(), name='chat-session-detail'),
    path(
        'sessions/<int:conversation_id>/messages/',
        views.MessageListView.as_view(),
        name='chat-session-messages',
    ),
    path(
        'messages/<int:message_id>/feedback/',
        views.MessageFeedbackView.as_view(),
        name='chat-message-feedback',
    ),
]
