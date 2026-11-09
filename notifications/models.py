from datetime import time

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


def _default_timezone() -> str:
    return getattr(settings, 'TIME_ZONE', 'Europe/Paris') or 'Europe/Paris'


class NotificationPreference(models.Model):
    """
    Réglages de notifications d'un utilisateur. Absence de ligne = valeurs par défaut du registre
    (créée paresseusement au premier PATCH). `categories` / `types` ne stockent que des surcharges.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_preference'
    )
    categories = models.JSONField(default=dict, blank=True, help_text='{catégorie: bool} (surcharges)')
    types = models.JSONField(default=dict, blank=True, help_text='{type: bool} (surcharges fines, ex. miams)')
    quiet_hours_start = models.TimeField(default=time(22, 0))
    quiet_hours_end = models.TimeField(default=time(8, 0))
    timezone = models.CharField(max_length=64, default=_default_timezone)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Préférences de notifications'
        verbose_name_plural = 'Préférences de notifications'

    def __str__(self):
        return f'Préférences notifications de {self.user_id}'


class NotificationDelivery(models.Model):
    """
    Une push logique (tous appareils confondus) pour un utilisateur : plafond quotidien,
    idempotence (`dedupe_key`) et taux d'ouverture par type (`opened_at`).
    Son id est envoyé dans `data.notification_id`.
    """

    STATUS_SENT = 'sent'
    STATUS_CAPPED = 'capped'
    STATUS_QUIET = 'quiet_hours'
    STATUS_CHOICES = [
        (STATUS_SENT, 'Envoyée'),
        (STATUS_CAPPED, 'Plafond quotidien atteint'),
        (STATUS_QUIET, 'Heures calmes'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notification_deliveries')
    type = models.CharField(max_length=48)
    category = models.CharField(max_length=32)
    transactional = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_SENT)
    title = models.CharField(max_length=200, blank=True, default='')
    body = models.TextField(blank=True, default='')
    data = models.JSONField(default=dict, blank=True)
    inbox_notification = models.ForeignKey(
        'accounts.Notification', on_delete=models.SET_NULL, null=True, blank=True, related_name='deliveries'
    )
    dedupe_key = models.CharField(max_length=120, blank=True, default='')
    device_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    opened_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Envoi de notification'
        verbose_name_plural = 'Envois de notifications'
        indexes = [
            models.Index(fields=['user', 'created_at'], name='notifdeliv_user_created_idx'),
            models.Index(fields=['type', 'created_at'], name='notifdeliv_type_created_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'dedupe_key'], condition=~Q(dedupe_key=''), name='notifdeliv_user_dedupe_uniq'
            ),
        ]

    def __str__(self):
        return f'{self.type} → {self.user_id} ({self.status})'
