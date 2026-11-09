from django.apps import AppConfig


class AppVersionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'appversion'
    verbose_name = "Versions de l'app"

    def ready(self):
        from . import signals  # noqa: F401
