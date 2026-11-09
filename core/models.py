from django.conf import settings
from django.db import models
from django.utils import timezone


class IdempotencyRecord(models.Model):
    """
    Réponse mémorisée d'une requête mutante envoyée avec `X-Idempotency-Key` / `Idempotency-Key`
    (`core.idempotency.idempotent`). Clé unique par utilisateur, conservée `IDEMPOTENCY_TTL` (48 h).
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    key = models.CharField(max_length=255)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=512)
    request_hash = models.CharField(max_length=64, help_text='sha256 du corps + paramètres de requête')
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'key'], name='idempotency_user_key_uniq'),
        ]
        indexes = [
            models.Index(fields=['created_at'], name='idempotency_created_idx'),
        ]

    def __str__(self):
        return f'{self.user_id} {self.method} {self.path} [{self.key}]'
