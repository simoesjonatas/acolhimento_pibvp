"""Resolve os templates padrao da Twilio: usa o valor salvo no banco (editado pela tela
de configuracao) e, se vazio, cai no valor do .env (settings). Centraliza o fallback para
que todos os consumidores (dashboard, continuar conversa, regras) leiam do mesmo lugar.
"""

from django.conf import settings

from apps.acolhimento.models import TemplateWhatsapp


# tipo -> (setting do SID, setting das variaveis) usados como fallback do .env
_FALLBACK = {
    TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO: (
        'TWILIO_TEMPLATE_OPT_IN_SID',
        'TWILIO_TEMPLATE_OPT_IN_VARIABLES',
    ),
    TemplateWhatsapp.Tipo.CONTINUAR: (
        'TWILIO_TEMPLATE_CONTINUAR_SID',
        'TWILIO_TEMPLATE_CONTINUAR_VARIABLES',
    ),
}


def _config(tipo):
    return TemplateWhatsapp.objects.filter(tipo=tipo).first()


def sid_para(tipo) -> str:
    """SID efetivo do template (banco tem prioridade; senao, o do .env)."""
    config = _config(tipo)
    if config and (config.content_sid or '').strip():
        return config.content_sid.strip()
    return (getattr(settings, _FALLBACK[tipo][0], '') or '').strip()


def variables_para(tipo) -> str:
    """Variaveis (string JSON) efetivas do template (banco tem prioridade; senao, o .env)."""
    config = _config(tipo)
    if config and (config.content_variables or '').strip():
        return config.content_variables
    return getattr(settings, _FALLBACK[tipo][1], '') or '{}'


def opt_in_sid() -> str:
    return sid_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_variables() -> str:
    return variables_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def continuar_sid() -> str:
    return sid_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_variables() -> str:
    return variables_para(TemplateWhatsapp.Tipo.CONTINUAR)
