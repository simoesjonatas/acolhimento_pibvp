from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.acolhimento.models import MensagemContato


# Janela de atendimento do WhatsApp: apos 24h sem resposta da pessoa, so e
# possivel reabrir a conversa com um template aprovado pela Meta.
JANELA_ATENDIMENTO_HORAS = 24

WHATSAPP_OPTIN_REQUIRED_ERROR = (
    'WhatsApp bloqueado: a pessoa ainda nao respondeu ao template de primeiro contato.'
)
WHATSAPP_JANELA_FECHADA_ERROR = (
    'WhatsApp bloqueado: a janela de 24h esta fechada (a pessoa nao responde ha mais de 24h). '
    'Envie o template de continuacao para reabrir a conversa antes de mandar mensagens livres.'
)


def pessoa_pode_receber_whatsapp(pessoa) -> bool:
    """A pessoa ja deu opt-in (respondeu ao template de primeiro contato) alguma vez."""
    return bool(getattr(pessoa, 'iniciou_interacao', False))


def ultima_entrada_em(pessoa):
    """Data/hora da ultima mensagem recebida (entrada) da pessoa, ou None."""
    return (
        pessoa.mensagens.filter(direcao=MensagemContato.DirecaoChoices.ENTRADA)
        .order_by('-enfileirada_em')
        .values_list('enfileirada_em', flat=True)
        .first()
    )


def janela_atendimento_aberta(pessoa) -> bool:
    """True se a pessoa enviou alguma mensagem nas ultimas 24h (janela do WhatsApp aberta)."""
    ultima = ultima_entrada_em(pessoa)
    if not ultima:
        return False
    return ultima >= timezone.now() - timedelta(hours=JANELA_ATENDIMENTO_HORAS)


def pode_enviar_livre(pessoa) -> bool:
    """Pode enviar uma mensagem livre (nao-template) agora: opt-in dado E janela aberta."""
    return pessoa_pode_receber_whatsapp(pessoa) and janela_atendimento_aberta(pessoa)


def precisa_template_continuar(pessoa) -> bool:
    """Ja deu opt-in mas a janela fechou: precisa do template de continuacao para reabrir."""
    return pessoa_pode_receber_whatsapp(pessoa) and not janela_atendimento_aberta(pessoa)


def motivo_bloqueio_livre(pessoa):
    """(codigo, mensagem) do bloqueio de envio livre, ou None se pode enviar."""
    if not pessoa_pode_receber_whatsapp(pessoa):
        return ('whatsapp_sem_interacao_previa', WHATSAPP_OPTIN_REQUIRED_ERROR)
    if not janela_atendimento_aberta(pessoa):
        return ('whatsapp_janela_24h_fechada', WHATSAPP_JANELA_FECHADA_ERROR)
    return None


def _sids_templates():
    sids = {
        (getattr(settings, 'TWILIO_TEMPLATE_OPT_IN_SID', '') or '').strip(),
        (getattr(settings, 'TWILIO_TEMPLATE_CONTINUAR_SID', '') or '').strip(),
    }
    return {sid for sid in sids if sid}


def mensagem_eh_template(mensagem: MensagemContato) -> bool:
    """Templates (opt-in ou continuacao) sao isentos da janela de 24h."""
    metadata = dict(mensagem.metadata_envio or {})
    if metadata.get('tipo_template') in ('primeiro_contato_opt_in', 'continuar_conversa'):
        return True
    template_cfg = dict(metadata.get('twilio_template') or {})
    content_sid = (template_cfg.get('content_sid') or '').strip()
    return bool(content_sid and content_sid in _sids_templates())


def mensagem_eh_template_primeiro_contato(mensagem: MensagemContato) -> bool:
    metadata = dict(mensagem.metadata_envio or {})
    if metadata.get('tipo_template') == 'primeiro_contato_opt_in':
        return True
    template_cfg = dict(metadata.get('twilio_template') or {})
    content_sid = (template_cfg.get('content_sid') or '').strip()
    opt_in_sid = (getattr(settings, 'TWILIO_TEMPLATE_OPT_IN_SID', '') or '').strip()
    return bool(content_sid and opt_in_sid and content_sid == opt_in_sid)


def _eh_whatsapp_saida_livre(mensagem: MensagemContato) -> bool:
    return (
        mensagem.canal == MensagemContato.CanalChoices.WHATSAPP
        and mensagem.direcao == MensagemContato.DirecaoChoices.SAIDA
        and not mensagem_eh_template(mensagem)
    )


def mensagem_whatsapp_exige_interacao_previa(mensagem: MensagemContato) -> bool:
    return _eh_whatsapp_saida_livre(mensagem)


def mensagem_pode_ser_enviada_no_whatsapp(mensagem: MensagemContato) -> bool:
    if not _eh_whatsapp_saida_livre(mensagem):
        return True
    return pode_enviar_livre(mensagem.pessoa)


def motivo_bloqueio_mensagem(mensagem: MensagemContato):
    """(codigo, mensagem) do bloqueio da mensagem, ou None. Usado pelo processador da fila."""
    if not _eh_whatsapp_saida_livre(mensagem):
        return None
    return motivo_bloqueio_livre(mensagem.pessoa)
