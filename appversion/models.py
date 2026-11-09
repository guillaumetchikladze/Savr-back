from django.core.exceptions import ValidationError
from django.db import models

from .versioning import parse_version

DEFAULT_MESSAGE_UPDATE_AVAILABLE = (
    'Une nouvelle version de Tchikook est disponible. Mets à jour l’app pour profiter des dernières nouveautés.'
)
DEFAULT_MESSAGE_UPDATE_REQUIRED = (
    'Cette version de Tchikook n’est plus prise en charge. Mets à jour l’app pour continuer à l’utiliser.'
)


class AppVersionPolicy(models.Model):
    """Politique de version par plateforme, éditable dans l'admin sans redéploiement.

    - latest_version : dernière version publiée sur le store → « update_available » en dessous ;
    - min_supported_version : plus vieille version acceptée → « update_required » (blocage) en dessous.
    """

    PLATFORM_IOS = 'ios'
    PLATFORM_ANDROID = 'android'
    PLATFORM_CHOICES = [(PLATFORM_IOS, 'iOS'), (PLATFORM_ANDROID, 'Android')]

    platform = models.CharField('Plateforme', max_length=16, choices=PLATFORM_CHOICES, unique=True)
    latest_version = models.CharField(
        'Dernière version', max_length=32, help_text='Version publiée sur le store (ex. 1.0.9).'
    )
    min_supported_version = models.CharField(
        'Version minimale supportée',
        max_length=32,
        help_text='En dessous, l’app affiche un écran bloquant de mise à jour (ex. 1.0.8).',
    )
    store_url = models.URLField('URL du store', max_length=500)
    message_update_available = models.TextField(
        'Message « mise à jour disponible »', blank=True, default=DEFAULT_MESSAGE_UPDATE_AVAILABLE
    )
    message_update_required = models.TextField(
        'Message « mise à jour obligatoire »', blank=True, default=DEFAULT_MESSAGE_UPDATE_REQUIRED
    )
    updated_at = models.DateTimeField('Mis à jour le', auto_now=True)

    class Meta:
        verbose_name = "Politique de version de l'app"
        verbose_name_plural = "Politiques de version de l'app"
        ordering = ['platform']

    def __str__(self):
        return f'{self.get_platform_display()} — latest {self.latest_version}, min {self.min_supported_version}'

    def clean(self):
        super().clean()
        errors = {}
        latest = parse_version(self.latest_version)
        minimum = parse_version(self.min_supported_version)
        if latest is None:
            errors['latest_version'] = 'Version invalide (format attendu : 1.0.9).'
        if minimum is None:
            errors['min_supported_version'] = 'Version invalide (format attendu : 1.0.8).'
        if latest is not None and minimum is not None and minimum > latest:
            errors['min_supported_version'] = 'La version minimale doit être inférieure ou égale à la dernière version.'
        if errors:
            raise ValidationError(errors)
