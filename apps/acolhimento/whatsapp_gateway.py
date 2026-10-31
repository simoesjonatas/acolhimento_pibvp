"""Gateway de WhatsApp: escolhe o provider (Meta, Twilio ou Evolution) em runtime.

Ponto unico usado pelo processador da fila (`fila_processor.py`). Mantem o
comportamento do Twilio identico ao anterior; WHATSAPP_PROVIDER=meta fala direto
com a API oficial (Cloud API) e segue as mesmas regras de opt-in/janela/template;
quando WHATSAPP_PROVIDER=evolution, envia texto simples (modo Baileys) e ignora
essas regras (que sao especificas da WhatsApp Business API).
"""
from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.acolhimento import (
    evolution_service,
    mensagem_variacao,
    meta_service,
    template_config,
    twilio_service,
)
from apps.acolhimento.models import ConfiguracaoProcessamentoFila, MensagemContato, TemplateWhatsapp
from apps.acolhimento import whatsapp_rules


class WhatsAppSendError(RuntimeError):
    """Erro unificado de envio, independente do provider."""


def provider() -> str:
    return (getattr(settings, 'WHATSAPP_PROVIDER', 'twilio') or 'twilio').strip().lower()


def usando_evolution() -> bool:
    return provider() == 'evolution'


def usando_meta() -> bool:
    return provider() == 'meta'


def provider_key() -> str:
    """Chave usada em metadata_envio para guardar o resultado/erro do provider."""
    if usando_evolution():
        return 'evolution'
    if usando_meta():
        return 'meta'
    return 'twilio'


def motivo_bloqueio_mensagem(mensagem: MensagemContato):
    """Regras de bloqueio (opt-in / janela 24h). No modo Baileys nao se aplicam."""
    if usando_evolution():
        return None
    return whatsapp_rules.motivo_bloqueio_mensagem(mensagem)


def _variar(texto: str, mensagem: MensagemContato) -> str:
    """Aplica variacao leve e deterministica (anti-bloqueio) ao texto de template.

    Seed = id da mensagem: cada pessoa recebe uma versao distinta do texto, e
    reenviar a mesma mensagem mantem o resultado estavel.
    """
    nome = getattr(mensagem.pessoa, 'nome', '') or ''
    return mensagem_variacao.variar_texto(texto, nome=nome, seed=mensagem.id)


def _delay_digitando_ms(texto: str) -> int:
    """Tempo de 'digitando...' (composing) antes do envio, conforme a configuracao."""
    cfg = ConfiguracaoProcessamentoFila.carregar()
    base = cfg.delay_digitando_ms()
    # Escala leve pelo tamanho do texto (mais texto = "digita" um pouco mais).
    extra = min(len(texto or '') * 20, 2000)
    return base + extra


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
        return _variar(texto, mensagem)
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
        return _variar(texto, mensagem)
    # Mensagem livre (ou template de marketing, que no Baileys deve usar modo livre):
    # o proprio conteudo ja e o texto a enviar.
    return mensagem.conteudo or ''


def _enviar_evolution(mensagem: MensagemContato, destino: str) -> dict[str, Any]:
    texto = _texto_para_evolution(mensagem)
    delay_ms = _delay_digitando_ms(texto)
    # Quem ja migrou para o LID do WhatsApp so recebe por ele: enviar para o telefone
    # volta com erro 463 e a mensagem nao chega. O LID e aprendido no webhook.
    lid = (getattr(mensagem.pessoa, 'whatsapp_lid', '') or '').strip()
    try:
        resultado = evolution_service.send_whatsapp_text(
            to_phone=destino, text=texto, delay_ms=delay_ms, lid=lid
        )
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


_TIPOS_TEMPLATE_CONFIGURAVEIS = {
    TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO,
    TemplateWhatsapp.Tipo.CONTINUAR,
}


def _template_meta_para(mensagem: MensagemContato) -> dict[str, Any]:
    """Bloco {nome, idioma, parametros} do template a enviar pela Meta, ou {} para texto livre.

    Prioridade: o que foi gravado na mensagem ao enfileirar (`meta_template`). Se a
    mensagem e de um fluxo configuravel (opt-in / continuar) mas foi enfileirada em
    outro provider (ex.: migracao Twilio -> Meta com fila pendente), resolve o
    template na hora pela configuracao atual.
    """
    metadata = dict(mensagem.metadata_envio or {})
    template = dict(metadata.get('meta_template') or {})
    if (template.get('nome') or '').strip():
        return template

    tipo = metadata.get('tipo_template')
    if tipo in _TIPOS_TEMPLATE_CONFIGURAVEIS:
        nome = template_config.meta_nome_para(tipo)
        if not nome:
            raise WhatsAppSendError(template_config.mensagem_nao_configurado(tipo))
        return template_config.montar_meta_template(tipo, mensagem.pessoa)
    return {}


def _enviar_meta(mensagem: MensagemContato, destino: str) -> dict[str, Any]:
    template = _template_meta_para(mensagem)
    try:
        if template:
            resultado = meta_service.send_template(
                to_phone=destino,
                nome=template['nome'],
                idioma=template.get('idioma') or 'pt_BR',
                parametros=template.get('parametros') or {},
            )
        else:
            resultado = meta_service.send_text(to_phone=destino, text=mensagem.conteudo)
    except meta_service.MetaWhatsAppError as exc:
        raise WhatsAppSendError(str(exc)) from exc

    # A Meta responde "accepted" assim que aceita a mensagem; entrega/leitura/falha
    # chegam depois pelo webhook (`meta_webhooks.py`), que atualiza a mensagem.
    return {
        'provider': 'meta',
        'status_fila': MensagemContato.StatusFilaChoices.ENVIADA,
        'enviada_em': timezone.now(),
        'referencia_externa': resultado.get('sid') or '',
        'status_label': str(resultado.get('status') or 'accepted'),
        'metadata_key': 'meta',
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
    if usando_meta():
        return _enviar_meta(mensagem, destino)
    return _enviar_twilio(mensagem, destino)
