from django.conf import settings

from apps.acolhimento.models import MensagemContato


WHATSAPP_OPTIN_REQUIRED_ERROR = (
    'WhatsApp bloqueado: a pessoa ainda nao respondeu ao template de primeiro contato.'
)


def pessoa_pode_receber_whatsapp(pessoa) -> bool:
    return bool(getattr(pessoa, 'iniciou_interacao', False))


def mensagem_eh_template_primeiro_contato(mensagem: MensagemContato) -> bool:
    metadata = dict(mensagem.metadata_envio or {})
    if metadata.get('tipo_template') == 'primeiro_contato_opt_in':
        return True

    template_cfg = dict(metadata.get('twilio_template') or {})
    content_sid = (template_cfg.get('content_sid') or '').strip()
    opt_in_sid = (getattr(settings, 'TWILIO_TEMPLATE_OPT_IN_SID', '') or '').strip()
    return bool(content_sid and opt_in_sid and content_sid == opt_in_sid)


def mensagem_whatsapp_exige_interacao_previa(mensagem: MensagemContato) -> bool:
    return (
        mensagem.canal == MensagemContato.CanalChoices.WHATSAPP
        and mensagem.direcao == MensagemContato.DirecaoChoices.SAIDA
        and not mensagem_eh_template_primeiro_contato(mensagem)
    )


def mensagem_pode_ser_enviada_no_whatsapp(mensagem: MensagemContato) -> bool:
    if not mensagem_whatsapp_exige_interacao_previa(mensagem):
        return True
    return pessoa_pode_receber_whatsapp(mensagem.pessoa)
