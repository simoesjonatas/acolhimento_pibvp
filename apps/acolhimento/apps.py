from django.apps import AppConfig


class AcolhimentoConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.acolhimento'

    def ready(self):
        # Registra os signals (processamento automatico da fila).
        from apps.acolhimento import signals  # noqa: F401
        # Registra os system checks (ex.: webhook sem segredo em producao).
        from apps.acolhimento import checks  # noqa: F401
