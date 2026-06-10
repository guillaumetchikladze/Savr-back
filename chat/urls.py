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
    path(
        'messages/<int:message_id>/feedback/',
        views.MessageFeedbackView.as_view(),
        name='chat-message-feedback',
    ),
]
