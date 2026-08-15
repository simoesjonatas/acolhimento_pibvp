"""System checks do app (rodam em `manage.py check` e `check --deploy`).

Servem para avisar cedo, no deploy, sobre configuracoes de ambiente que deixariam
o sistema inseguro ou inoperante em producao.
"""
from __future__ import annotations

from django.conf import settings
from django.core.checks import Warning, register


@register('security')
def webhook_secret_configurado(app_configs, **kwargs):
    """Avisa se o webhook do WhatsApp ficaria aberto/recusando em producao.

    Com WHATSAPP_PROVIDER=evolution e DEBUG=False, o endpoint de webhook exige o
    EVOLUTION_WEBHOOK_SECRET (ver `evolution_webhooks._webhook_autorizado`). Sem ele,
    as mensagens recebidas sao recusadas — melhor descobrir no deploy do que em runtime.
    """
    provider = (getattr(settings, 'WHATSAPP_PROVIDER', '') or '').strip().lower()
    segredo = (getattr(settings, 'EVOLUTION_WEBHOOK_SECRET', '') or '').strip()

    if settings.DEBUG or provider != 'evolution' or segredo:
        return []

    return [
        Warning(
            'EVOLUTION_WEBHOOK_SECRET nao configurado com WHATSAPP_PROVIDER=evolution '
            'em producao: o webhook recusara todas as mensagens recebidas (403).',
            hint=(
                'Defina EVOLUTION_WEBHOOK_SECRET no ambiente e reconfigure a instancia '
                'do Evolution (tela de configuracao do WhatsApp / create_instance) para '
                'que ela passe a enviar o header X-Evolution-Webhook-Secret.'
            ),
            id='acolhimento.W001',
        )
    ]
