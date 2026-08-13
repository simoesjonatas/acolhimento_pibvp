"""Gateway de WhatsApp: escolhe o provider (Twilio ou Evolution) em runtime.

Ponto unico usado pelo processador da fila (`fila_processor.py`). Mantem o
comportamento do Twilio identico ao anterior; quando WHATSAPP_PROVIDER=evolution,
envia texto simples (modo Baileys) e ignora as regras de opt-in/janela/template
da Meta (que sao especificas da Twilio/WhatsApp Business API).
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.acolhimento import evolution_service, template_config, twilio_service
from apps.acolhimento.models import MensagemContato, TemplateWhatsapp
from apps.acolhimento import whatsapp_rules


class WhatsAppSendError(RuntimeError):
    """Erro unificado de envio, independente do provider."""


def provider() -> str:
    return (getattr(settings, 'WHATSAPP_PROVIDER', 'twilio') or 'twilio').strip().lower()


def usando_evolution() -> bool:
    return provider() == 'evolution'


def provider_key() -> str:
    """Chave usada em metadata_envio para guardar o resultado/erro do provider."""
    return 'evolution' if usando_evolution() else 'twilio'


def motivo_bloqueio_mensagem(mensagem: MensagemContato):
    """Regras de bloqueio (opt-in / janela 24h). No modo Baileys nao se aplicam."""
    if usando_evolution():
        return None
    return whatsapp_rules.motivo_bloqueio_mensagem(mensagem)


def _texto_para_evolution(mensagem: MensagemContato) -> str:
    """No Baileys nao ha template: 'traduz' as mensagens de template em texto simples."""
    metadata = dict(mensagem.metadata_envio or {})
    tipo = metadata.get('tipo_template')

    if tipo == 'primeiro_contato_opt_in':
        texto = (metadata.get('evolution_texto') or '').strip()
        if not texto:
            texto = template_config.renderizar_texto_evolution(
                TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO,
                mensagem.pessoa,
            )
        if not texto:
            raise WhatsAppSendError(
                'Mensagem de boas-vindas do WhatsApp nao configurada. '
                'Defina o texto na tela de configuracao de templates.'
            )
        return texto
    if tipo == 'continuar_conversa':
        texto = (metadata.get('evolution_texto') or '').strip()
        if not texto:
            texto = template_config.renderizar_texto_evolution(
                TemplateWhatsapp.Tipo.CONTINUAR,
                mensagem.pessoa,
            )
        if not texto:
            raise WhatsAppSendError(
                'Mensagem de continuacao do WhatsApp nao configurada. '
                'Defina o texto na tela de configuracao de templates.'
            )
        return texto
    # Mensagem livre (ou template de marketing, que no Baileys deve usar modo livre):
    # o proprio conteudo ja e o texto a enviar.
    return mensagem.conteudo or ''


def _enviar_evolution(mensagem: MensagemContato, destino: str) -> dict[str, Any]:
    texto = _texto_para_evolution(mensagem)
    try:
        resultado = evolution_service.send_whatsapp_text(to_phone=destino, text=texto)
    except evolution_service.EvolutionWhatsAppError as exc:
        raise WhatsAppSendError(str(exc)) from exc

    return {
        'provider': 'evolution',
        'status_fila': MensagemContato.StatusFilaChoices.ENVIADA,
        'enviada_em': timezone.now(),
        'referencia_externa': resultado.get('sid') or '',
        'status_label': str(resultado.get('status') or 'PENDING'),
        'metadata_key': 'evolution',
        'metadata_value': resultado,
    }


def _enviar_twilio(mensagem: MensagemContato, destino: str) -> dict[str, Any]:
    template_cfg = dict((mensagem.metadata_envio or {}).get('twilio_template') or {})
    content_sid = (template_cfg.get('content_sid') or '').strip()
    content_variables = (template_cfg.get('content_variables') or '').strip() if template_cfg else None

    try:
        if content_sid:
            resultado = twilio_service.send_whatsapp_message(
                to_phone=destino,
                content_sid=content_sid,
                content_variables=content_variables,
            )
        else:
            resultado = twilio_service.send_whatsapp_message(to_phone=destino, body=mensagem.conteudo)
    except twilio_service.TwilioWhatsAppError as exc:
        raise WhatsAppSendError(str(exc)) from exc

    status = (resultado.get('status') or '').lower()
    if status in {'queued', 'accepted', 'sending'}:
        status_fila = MensagemContato.StatusFilaChoices.PROCESSANDO
        enviada_em = None
    else:
        status_fila = MensagemContato.StatusFilaChoices.ENVIADA
        enviada_em = timezone.now()

    return {
        'provider': 'twilio',
        'status_fila': status_fila,
        'enviada_em': enviada_em,
        'referencia_externa': resultado.get('sid', '') or '',
        'status_label': status or 'desconhecido',
        'metadata_key': 'twilio',
        'metadata_value': resultado,
    }


def enviar(mensagem: MensagemContato, destino: str) -> dict[str, Any]:
    """Envia a mensagem pelo provider ativo. Levanta WhatsAppSendError em falha.

    Retorna um envelope normalizado que o processador da fila aplica direto:
    provider, status_fila, enviada_em, referencia_externa, status_label,
    metadata_key, metadata_value.
    """
    if usando_evolution():
        return _enviar_evolution(mensagem, destino)
    return _enviar_twilio(mensagem, destino)
