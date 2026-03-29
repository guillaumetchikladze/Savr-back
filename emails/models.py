from django.conf import settings
from django.db import models


class EmailStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    # Réservés pour plus tard (webhooks Graph / tracking)
    DELIVERED = "DELIVERED", "Delivered"
    BOUNCED = "BOUNCED", "Bounced"
    OPENED = "OPENED", "Opened"
    CLICKED = "CLICKED", "Clicked"
    COMPLAINED = "COMPLAINED", "Complained"
    DELIVERY_DELAYED = "DELIVERY_DELAYED", "Delivery delayed"


class EmailPriority(models.TextChoices):
    LOW = "LOW", "Low"
    NORMAL = "NORMAL", "Normal"
    HIGH = "HIGH", "High"
    URGENT = "URGENT", "Urgent"


class EmailQueue(models.Model):
    action_name = models.CharField(max_length=128, blank=True, null=True)
    priority = models.CharField(
        max_length=16, choices=EmailPriority.choices, default=EmailPriority.NORMAL
    )
    status = models.CharField(
        max_length=32, choices=EmailStatus.choices, default=EmailStatus.PENDING
    )

    from_email = models.EmailField()
    to_email = models.EmailField()
    subject = models.CharField(max_length=255)

    # Convention:
    # - template_name: "emails/welcome" (sans extension) ou "emails/welcome.html"
    # - context: dict sérialisable JSON
    # - html/text optionnels si tu veux envoyer du contenu brut
    content = models.JSONField(default=dict, blank=True)

    retries = models.PositiveIntegerField(default=0)
    max_retries = models.PositiveIntegerField(default=3)
    error = models.TextField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )

    class Meta:
        db_table = "email_queue"
        indexes = [
            models.Index(fields=["priority", "status", "created_at"], name="emailq_pri_st_created_idx"),
            models.Index(fields=["status", "created_at"], name="emailq_st_created_idx"),
            models.Index(fields=["created_at"], name="emailq_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.status} {self.priority} to={self.to_email} subject={self.subject}"

