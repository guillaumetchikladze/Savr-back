from django.conf import settings
from django.db import models

from .food_groups import FOOD_GROUP_CHOICES


class IngredientProfile(models.Model):
    """Enrichissement « stats » d'un ingrédient : groupe alimentaire, végétal, ingrédient de base."""

    SOURCE_LLM = 'llm'
    SOURCE_MANUAL = 'manual'
    SOURCE_CIQUAL = 'ciqual'
    SOURCE_CHOICES = [
        (SOURCE_LLM, 'LLM'),
        (SOURCE_MANUAL, 'Manuel / règles'),
        (SOURCE_CIQUAL, 'Ciqual (ANSES)'),
    ]

    ingredient = models.OneToOneField('recipes.Ingredient', on_delete=models.CASCADE, related_name='profile')
    food_group = models.CharField(max_length=32, choices=FOOD_GROUP_CHOICES, default='autres', db_index=True)
    is_plant = models.BooleanField(default=False)
    is_basic_staple = models.BooleanField(
        default=False, help_text='Sel, poivre, huile, eau, sucre, farine, épices sèches… : exclu de la diversité.'
    )
    source = models.CharField(max_length=16, choices=SOURCE_CHOICES, default=SOURCE_LLM)
    validated = models.BooleanField(default=False, help_text='Relu par un humain : jamais reclassé automatiquement.')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Profil ingrédient (stats)'
        verbose_name_plural = 'Profils ingrédients (stats)'

    def __str__(self):
        return f'{self.ingredient_id} → {self.food_group}'


class UserBadge(models.Model):
    """Badge obtenu (définitions dans `stats.services.badges.BADGES`, persisté une seule fois)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='badges')
    code = models.CharField(max_length=64)
    earned_at = models.DateTimeField(auto_now_add=True)
    context = models.JSONField(default=dict, blank=True)
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-earned_at', 'code']
        constraints = [models.UniqueConstraint(fields=['user', 'code'], name='userbadge_user_code_uniq')]

    def __str__(self):
        return f'{self.user_id} · {self.code}'


class BadgeRarity(models.Model):
    """
    Rareté d'un badge (tâche quotidienne `stats.tasks.compute_badge_rarity_task`) : part des cuisiniers
    actifs (≥ 1 « cuisiné » sur 90 jours) qui l'ont obtenu. Une ligne par code du registre.
    """

    code = models.CharField(max_length=64, primary_key=True)
    holders = models.PositiveIntegerField(default=0, help_text='Cuisiniers actifs qui ont ce badge.')
    active_users = models.PositiveIntegerField(default=0, help_text='Cuisiniers actifs au moment du calcul.')
    percent = models.FloatField(default=0.0)
    tier = models.CharField(max_length=16, default='commun')
    computed_at = models.DateTimeField()

    class Meta:
        verbose_name = 'Rareté de badge'
        verbose_name_plural = 'Raretés des badges'

    def __str__(self):
        return f'{self.code} · {self.percent:.1f} % ({self.tier})'
