import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

DEFAULT_CONVERSATION_TITLE = 'Nouvelle conversation'


class ConversationQuerySet(models.QuerySet):
    def alive(self):
        """Conversations non supprimées (la suppression utilisateur est douce, purgée plus tard)."""
        return self.filter(deleted_at__isnull=True)


class Conversation(models.Model):
    TITLE_DEFAULT = 'default'
    TITLE_AUTO = 'auto'
    TITLE_LLM = 'llm'
    TITLE_USER = 'user'
    TITLE_SOURCE_CHOICES = [
        (TITLE_DEFAULT, 'Titre par défaut'),
        (TITLE_AUTO, 'Premier message (tronqué)'),
        (TITLE_LLM, 'Généré par le LLM'),
        (TITLE_USER, 'Renommé par l’utilisateur'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='chat_conversations',
    )
    title = models.CharField(max_length=200, default=DEFAULT_CONVERSATION_TITLE)
    title_source = models.CharField(max_length=10, choices=TITLE_SOURCE_CHOICES, default=TITLE_DEFAULT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Supprimée par l’utilisateur (masquée partout, purgée après CHAT_DELETED_RETENTION_DAYS).',
    )

    objects = ConversationQuerySet.as_manager()

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['user', '-updated_at'], name='chat_conv_user_updated_idx'),
        ]

    def __str__(self):
        return f'{self.title} ({self.user_id})'


class Message(models.Model):
    ROLE_USER = 'user'
    ROLE_ASSISTANT = 'assistant'
    ROLE_SYSTEM = 'system'
    ROLE_CHOICES = [
        (ROLE_USER, 'User'),
        (ROLE_ASSISTANT, 'Assistant'),
        (ROLE_SYSTEM, 'System'),
    ]

    TYPE_TEXT = 'text'
    TYPE_MUTATION_PROPOSAL = 'mutation_proposal'
    TYPE_TOOL_TRACE = 'tool_trace'
    TYPE_IMPORT_JOB = 'import_job'
    TYPE_SYSTEM_EVENT = 'system_event'
    MESSAGE_TYPE_CHOICES = [
        (TYPE_TEXT, 'Text'),
        (TYPE_MUTATION_PROPOSAL, 'Mutation proposal'),
        (TYPE_TOOL_TRACE, 'Tool trace'),
        (TYPE_IMPORT_JOB, 'Import job'),
        (TYPE_SYSTEM_EVENT, 'System event'),
    ]

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    message_type = models.CharField(max_length=30, choices=MESSAGE_TYPE_CHOICES, default=TYPE_TEXT)
    content = models.TextField(blank=True)
    ui_payload = models.JSONField(null=True, blank=True)
    turn_id = models.UUIDField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.role}/{self.message_type} in conv {self.conversation_id}'


class MessageFeedback(models.Model):
    RATING_UP = 'up'
    RATING_DOWN = 'down'
    RATING_CHOICES = [
        (RATING_UP, 'Thumbs up'),
        (RATING_DOWN, 'Thumbs down'),
    ]

    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name='feedbacks',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='chat_message_feedbacks',
    )
    rating = models.CharField(max_length=10, choices=RATING_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['message', 'user'], name='unique_message_feedback_per_user'),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.rating} on message {self.message_id} by {self.user_id}'


class PendingAction(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_EXPIRED = 'expired'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_CONFIRMED, 'Confirmed'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_EXPIRED, 'Expired'),
    ]

    ACTION_MEAL_DELETION = 'meal_deletion'
    ACTION_MEAL_INVITATION = 'meal_invitation'
    ACTION_RECIPE_REVISION = 'recipe_revision'
    ACTION_TYPE_CHOICES = [
        (ACTION_MEAL_DELETION, 'Meal deletion'),
        (ACTION_MEAL_INVITATION, 'Meal invitation'),
        (ACTION_RECIPE_REVISION, 'Recipe revision'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='pending_actions',
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name='pending_actions',
    )
    action_type = models.CharField(max_length=40, choices=ACTION_TYPE_CHOICES)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def is_expired(self):
        return timezone.now() >= self.expires_at

    def mark_expired_if_needed(self):
        if self.status == self.STATUS_PENDING and self.is_expired():
            self.status = self.STATUS_EXPIRED
            self.save(update_fields=['status'])
            return True
        return False
