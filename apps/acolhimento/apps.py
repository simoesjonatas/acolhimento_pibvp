from django.apps import AppConfig


class AcolhimentoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.acolhimento'

    def ready(self):
        # Registra os signals (processamento automatico da fila).
        from apps.acolhimento import signals  # noqa: F401
