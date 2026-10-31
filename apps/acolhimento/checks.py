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


@register('security')
def meta_cloud_api_configurada(app_configs, **kwargs):
    """Com WHATSAPP_PROVIDER=meta, avisa cedo o que falta para a API oficial funcionar.

    - Sem token/phone number id: nenhum envio sai (aviso).
    - Sem App Secret em producao: o webhook recusa todos os eventos (aviso), igual ao
      caso do Evolution em `webhook_secret_configurado`.
    - Sem verify token: a Meta nao consegue validar a URL do webhook (aviso).
    """
    provider = (getattr(settings, 'WHATSAPP_PROVIDER', '') or '').strip().lower()
    if provider != 'meta':
        return []

    problemas = []
    token = (getattr(settings, 'META_WA_ACCESS_TOKEN', '') or '').strip()
    phone_id = (getattr(settings, 'META_WA_PHONE_NUMBER_ID', '') or '').strip()
    if not token or not phone_id:
        problemas.append(
            Warning(
                'WHATSAPP_PROVIDER=meta sem META_WA_ACCESS_TOKEN e/ou META_WA_PHONE_NUMBER_ID: '
                'nenhuma mensagem de WhatsApp sera enviada.',
                hint='Copie o Phone Number ID e gere um token permanente de System User no painel da Meta.',
                id='acolhimento.W002',
            )
        )

    if not settings.DEBUG and not (getattr(settings, 'META_WA_APP_SECRET', '') or '').strip():
        problemas.append(
            Warning(
                'META_WA_APP_SECRET nao configurado com WHATSAPP_PROVIDER=meta em producao: '
                'o webhook recusara todos os eventos recebidos (403).',
                hint='Defina META_WA_APP_SECRET com o App Secret do app em developers.facebook.com.',
                id='acolhimento.W003',
            )
        )

    if not (getattr(settings, 'META_WA_VERIFY_TOKEN', '') or '').strip():
        problemas.append(
            Warning(
                'META_WA_VERIFY_TOKEN nao configurado: a Meta nao consegue validar a URL do webhook.',
                hint='Escolha uma string secreta, defina META_WA_VERIFY_TOKEN e use a mesma no painel da Meta.',
                id='acolhimento.W004',
            )
        )
    return problemas
