from django.contrib.auth.models import AbstractUser
from django.db import models


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
        ('follow', 'Nouveau complice'),
        ('recipe_reminder', 'Rappel de recette'),
        ('recipe_shared', 'Recette partagée'),
        ('achievement', 'Nouveau succès'),
        ('meal_invitation', 'Invitation à un repas'),
        ('photo_during_cooking', 'Photo pendant la cuisine'),
        ('photo_after_cooking', 'Photo après la cuisine'),
        ('photo_at_meal_time', 'Photo à l\'heure du repas'),
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
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Créé le')
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Notification'
        verbose_name_plural = 'Notifications'
    
    def __str__(self):
        return f"{self.title} - {self.user.username}"

