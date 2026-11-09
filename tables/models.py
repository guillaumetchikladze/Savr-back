"""
« Les tablées » (.claude-work/specs/tables.md) : petit groupe privé de 2 à 8 personnes (amis, famille,
colocs) avec un compteur **collectif** de la semaine, le dernier plat de chacun et le rituel du
dimanche « Qui cuisine quoi cette semaine ? ». Jamais de classement individuel.
"""

from django.conf import settings
from django.db import models


class Table(models.Model):
    name = models.CharField(max_length=60)
    emoji = models.CharField(max_length=16, blank=True, default='')
    color = models.CharField(max_length=16, blank=True, default='', help_text='Couleur « #RRGGBB » (optionnelle).')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_tables',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at', 'id']
        verbose_name = 'Tablée'
        verbose_name_plural = 'Tablées'

    def __str__(self):
        return self.name


class TableMember(models.Model):
    """
    Appartenance consentie : `invited` (invitation d'un membre, à accepter ou refuser) → `accepted`.
    Un refus ou un départ supprime la ligne. `share_basket` : le membre partage ses envies non datées
    de la semaine avec la tablée (rituel du dimanche) ; désactivé par défaut.
    """

    ROLE_OWNER = 'owner'
    ROLE_MEMBER = 'member'
    ROLE_CHOICES = [(ROLE_OWNER, 'Créateur·rice'), (ROLE_MEMBER, 'Membre')]
    STATUS_INVITED = 'invited'
    STATUS_ACCEPTED = 'accepted'
    STATUS_CHOICES = [(STATUS_INVITED, 'Invité·e'), (STATUS_ACCEPTED, 'Membre')]

    table = models.ForeignKey(Table, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='table_memberships')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_INVITED)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    share_basket = models.BooleanField(
        default=False, help_text='Partage ses envies non datées de la semaine avec la tablée (rituel du dimanche).'
    )
    joined_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['joined_at', 'id']
        verbose_name = 'Membre de tablée'
        verbose_name_plural = 'Membres de tablée'
        constraints = [
            models.UniqueConstraint(fields=['table', 'user'], name='tablemember_table_user_uniq'),
        ]
        indexes = [
            models.Index(fields=['user', 'status'], name='tablemember_user_status_idx'),
        ]

    def __str__(self):
        return f'{self.user_id} @ {self.table_id} ({self.status})'
