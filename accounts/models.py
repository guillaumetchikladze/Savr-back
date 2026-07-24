from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings


class User(AbstractUser):
    """Custom User model"""
    email = models.EmailField(unique=True)
    username = models.CharField(max_length=150, unique=True)
    avatar_url = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Gamification fields
    level = models.IntegerField(default=1)
    experience_points = models.IntegerField(default=0)
    
    # Shopping list preferences
    default_shopping_list_days = models.IntegerField(default=7, help_text="Nombre de jours par défaut pour la liste de courses")

    # Préférences alimentaires (listes de libellés, ex. tags)
    food_dislikes = models.JSONField(default=list, blank=True)
    allergies = models.JSONField(default=list, blank=True)
    # Régimes: enum strings, séparé des listes (pas d’auto-mapping côté user prefs).
    # Exemples attendus: ["vegetarian", "vegan", "gluten_free", "halal", "kosher"]
    regimes = models.JSONField(default=list, blank=True)
    is_vegetarian = models.BooleanField(
        default=False,
        help_text="Régime végétarien (repérage viande/poisson côté recettes, en complément des listes).",
    )
    onboarding_completed = models.BooleanField(default=False)
    
    # Favorite recipes
    favorite_recipes = models.ManyToManyField(
        'recipes.Recipe',
        related_name='favorited_by',
        blank=True,
        help_text="Recettes favorites de l'utilisateur"
    )
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']
    
    def __str__(self):
        return self.email
    
    @property
    def followers_count(self):
        """Nombre de complices (followers)"""
        return self.followers.count()
    
    @property
    def following_count(self):
        """Nombre d'utilisateurs suivis"""
        return self.following.count()


class FollowRequest(models.Model):
    """Demande de suivi en attente d'acceptation."""

    STATUS_CHOICES = [
        ('pending', 'En attente'),
        ('accepted', 'Acceptée'),
        ('declined', 'Refusée'),
    ]

    requester = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='sent_follow_requests',
        verbose_name='Demandeur',
    )
    target = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='received_follow_requests',
        verbose_name='Cible',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        verbose_name='Statut',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['requester', 'target']
        ordering = ['-created_at']
        verbose_name = 'Demande de suivi'
        verbose_name_plural = 'Demandes de suivi'
        indexes = [
            models.Index(fields=['target', 'status'], name='followreq_target_status_idx'),
            models.Index(fields=['requester', 'status'], name='followreq_requester_status_idx'),
        ]

    def __str__(self):
        return f"{self.requester.username} → {self.target.username} ({self.status})"


class Follow(models.Model):
    """Relation de suivi entre utilisateurs (devenir complice)"""
    follower = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='following',
        verbose_name='Complice'
    )
    following = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='followers',
        verbose_name='Suivi'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['follower', 'following']
        ordering = ['-created_at']
        verbose_name = 'Relation de complice'
        verbose_name_plural = 'Relations de complices'
    
    def __str__(self):
        return f"{self.follower.username} suit {self.following.username}"


class Notification(models.Model):
    """Notifications pour les utilisateurs"""
    NOTIFICATION_TYPES = [
        ('follow', 'Nouvel ami'),
        ('follow_request', 'Demande d\'ami'),
        ('recipe_reminder', 'Rappel de recette'),
        ('recipe_shared', 'Recette partagée'),
        ('achievement', 'Nouveau succès'),
        ('meal_invitation', 'Invitation à un repas'),
        ('photo_during_cooking', 'Photo pendant la cuisine'),
        ('photo_after_cooking', 'Photo après la cuisine'),
        ('photo_at_meal_time', 'Photo à l\'heure du repas'),
        ('post_miam', 'Miam sur votre post'),
        ('post_comment', 'Commentaire sur votre post'),
        ('post_comment_mention', 'Vous êtes mentionné dans un commentaire'),
        ('post_repost', 'Repost de votre post'),
    ]
    
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name='Utilisateur'
    )
    notification_type = models.CharField(
        max_length=50,
        choices=NOTIFICATION_TYPES,
        verbose_name='Type de notification'
    )
    title = models.CharField(max_length=200, verbose_name='Titre')
    message = models.TextField(verbose_name='Message')
    related_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='sent_notifications',
        blank=True,
        null=True,
        verbose_name='Utilisateur lié'
    )
    is_read = models.BooleanField(default=False, verbose_name='Lu')
    related_post_id = models.IntegerField(null=True, blank=True, verbose_name='ID du post lié')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Créé le')
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Notification'
        verbose_name_plural = 'Notifications'
        indexes = [
            models.Index(fields=['user', 'created_at'], name='notif_user_created_at_idx'),
            models.Index(fields=['user', 'is_read'], name='notif_user_is_read_idx'),
        ]
    
    def __str__(self):
        return f"{self.title} - {self.user.username}"


class PushDevice(models.Model):
    """Appareil capable de recevoir des notifications push Expo."""
    PLATFORM_CHOICES = [
        ('android', 'Android'),
        ('ios', 'iOS'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='push_devices',
        verbose_name='Utilisateur',
    )
    expo_push_token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Appareil push'
        verbose_name_plural = 'Appareils push'

    def __str__(self):
        return f"{self.user.email} - {self.platform} - {self.expo_push_token}"


class LoyaltyCard(models.Model):
    """Carte de fidélité associée à un utilisateur, numéro chiffré côté serveur."""

    BARCODE_TYPE_CHOICES = [
        ('ean13', 'EAN-13'),
        ('code128', 'Code 128'),
        ('qr', 'QR Code'),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='loyalty_cards',
        verbose_name='Propriétaire',
    )
    name = models.CharField(max_length=100, verbose_name='Nom de la carte')
    emoji = models.CharField(
        max_length=16,
        blank=True,
        verbose_name='Emoji',
        help_text="Petit emoji pour identifier la carte dans l’UI",
    )
    barcode_type = models.CharField(
        max_length=32,
        choices=BARCODE_TYPE_CHOICES,
        default='code128',
        verbose_name='Type de code barre',
    )
    encrypted_number = models.TextField(
        verbose_name='Numéro chiffré',
        help_text="Numéro de carte chiffré avec une clé serveur (non lisible en base).",
    )
    number_last4 = models.CharField(
        max_length=8,
        blank=True,
        verbose_name='4 derniers chiffres',
        help_text="4 derniers chiffres pour affichage (non sensible).",
    )
    is_active = models.BooleanField(default=True, verbose_name='Active')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Carte de fidélité'
        verbose_name_plural = 'Cartes de fidélité'
        indexes = [
            models.Index(fields=['owner', 'is_active'], name='loyaltycard_owner_active_idx'),
        ]

    def __str__(self):
        suffix = f" • ••••{self.number_last4}" if self.number_last4 else ""
        return f"{self.name}{suffix} ({self.owner.email})"

