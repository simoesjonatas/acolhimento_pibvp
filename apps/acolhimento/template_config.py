"""Resolve as mensagens padrao do WhatsApp.

Usa o valor salvo no banco (editado pela tela de configuracao) e, se vazio,
cai no valor do .env (settings). Na Twilio os templates usam SID/variaveis; no
Evolution os mesmos fluxos usam texto simples.
"""

import json

from django.conf import settings

from apps.acolhimento.models import TemplateWhatsapp


# tipo -> settings usados como fallback do .env
_FALLBACK = {
    TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO: {
        'sid': 'TWILIO_TEMPLATE_OPT_IN_SID',
        'variables': 'TWILIO_TEMPLATE_OPT_IN_VARIABLES',
        'texto_evolution': 'EVOLUTION_TEXTO_OPTIN',
    },
    TemplateWhatsapp.Tipo.CONTINUAR: {
        'sid': 'TWILIO_TEMPLATE_CONTINUAR_SID',
        'variables': 'TWILIO_TEMPLATE_CONTINUAR_VARIABLES',
        'texto_evolution': 'EVOLUTION_TEXTO_CONTINUAR',
    },
}


def _config(tipo):
    return TemplateWhatsapp.objects.filter(tipo=tipo).first()


def provider() -> str:
    return (getattr(settings, 'WHATSAPP_PROVIDER', 'twilio') or 'twilio').strip().lower()


def usando_evolution() -> bool:
    return provider() == 'evolution'


def sid_para(tipo) -> str:
    """SID efetivo do template (banco tem prioridade; senao, o do .env)."""
    config = _config(tipo)
    if config and (config.content_sid or '').strip():
        return config.content_sid.strip()
    return (getattr(settings, _FALLBACK[tipo]['sid'], '') or '').strip()


def _json_objeto_valido(valor: str) -> bool:
    try:
        return isinstance(json.loads(valor), dict)
    except ValueError:
        return False


def variables_para(tipo) -> str:
    """Variaveis (string JSON) efetivas do template (banco tem prioridade; senao, o .env).

    O valor do .env passa por uma checagem de JSON: um valor quebrado no ambiente
    (ex.: `{}}`) chegava ate a tela de configuracao e reprovava o formulario, travando
    o salvamento de campos que nao tem nada a ver — e ninguem consegue corrigir um
    env var pela interface. O valor salvo no banco vai como esta, para que um JSON
    invalido digitado na tela seja apontado e possa ser corrigido ali mesmo.
    """
    config = _config(tipo)
    if config and (config.content_variables or '').strip():
        return config.content_variables
    do_env = getattr(settings, _FALLBACK[tipo]['variables'], '') or '{}'
    return do_env if _json_objeto_valido(do_env) else '{}'


def texto_evolution_para(tipo) -> str:
    """Texto efetivo usado pelo Evolution para um fluxo de template."""
    config = _config(tipo)
    if config and (config.texto_evolution or '').strip():
        return config.texto_evolution.strip()
    return (getattr(settings, _FALLBACK[tipo]['texto_evolution'], '') or '').strip()


def renderizar_texto_evolution(tipo, pessoa) -> str:
    nome = (getattr(pessoa, 'nome', '') or '').strip() or 'amigo(a)'
    return texto_evolution_para(tipo).replace('{nome}', nome)


def configurado_para(tipo) -> bool:
    """True quando o provider ativo tem a configuracao minima para enviar."""
    if usando_evolution():
        return bool(texto_evolution_para(tipo))
    return bool(sid_para(tipo))


def opt_in_sid() -> str:
    return sid_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_variables() -> str:
    return variables_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_texto_evolution() -> str:
    return texto_evolution_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_configurado() -> bool:
    return configurado_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def continuar_sid() -> str:
    return sid_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_variables() -> str:
    return variables_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_texto_evolution() -> str:
    return texto_evolution_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_configurado() -> bool:
    return configurado_para(TemplateWhatsapp.Tipo.CONTINUAR)
