from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'chat'
    verbose_name = 'Chat Agent'

    def ready(self):
        from chat.services.tracing import apply_tracing_setting

        apply_tracing_setting()
