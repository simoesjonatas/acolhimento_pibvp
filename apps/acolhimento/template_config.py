"""Resolve as mensagens padrao do WhatsApp.

Usa o valor salvo no banco (editado pela tela de configuracao) e, se vazio,
cai no valor do .env (settings). Na Twilio os templates usam SID/variaveis; na
API oficial da Meta usam nome + idioma (e as mesmas variaveis); no Evolution os
mesmos fluxos usam texto simples.
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
        'meta_nome': 'META_TEMPLATE_OPT_IN_NOME',
        'meta_idioma': 'META_TEMPLATE_OPT_IN_IDIOMA',
        'meta_variables': 'META_TEMPLATE_OPT_IN_VARIABLES',
    },
    TemplateWhatsapp.Tipo.CONTINUAR: {
        'sid': 'TWILIO_TEMPLATE_CONTINUAR_SID',
        'variables': 'TWILIO_TEMPLATE_CONTINUAR_VARIABLES',
        'texto_evolution': 'EVOLUTION_TEXTO_CONTINUAR',
        'meta_nome': 'META_TEMPLATE_CONTINUAR_NOME',
        'meta_idioma': 'META_TEMPLATE_CONTINUAR_IDIOMA',
        'meta_variables': 'META_TEMPLATE_CONTINUAR_VARIABLES',
    },
}

PROVIDER_META = 'meta'
PROVIDER_TWILIO = 'twilio'
PROVIDER_EVOLUTION = 'evolution'


def _config(tipo):
    return TemplateWhatsapp.objects.filter(tipo=tipo).first()


def provider() -> str:
    return (getattr(settings, 'WHATSAPP_PROVIDER', 'twilio') or 'twilio').strip().lower()


def usando_evolution() -> bool:
    return provider() == PROVIDER_EVOLUTION


def usando_meta() -> bool:
    return provider() == PROVIDER_META


def usando_twilio() -> bool:
    return provider() not in (PROVIDER_EVOLUTION, PROVIDER_META)


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
    chave_env = 'meta_variables' if usando_meta() else 'variables'
    do_env = getattr(settings, _FALLBACK[tipo][chave_env], '') or '{}'
    return do_env if _json_objeto_valido(do_env) else '{}'


def variaveis_dict_para(tipo) -> dict:
    """As variaveis como dict (nunca levanta: JSON invalido => {})."""
    try:
        parsed = json.loads(variables_para(tipo) or '{}')
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def texto_evolution_para(tipo) -> str:
    """Texto efetivo usado pelo Evolution para um fluxo de template."""
    config = _config(tipo)
    if config and (config.texto_evolution or '').strip():
        return config.texto_evolution.strip()
    return (getattr(settings, _FALLBACK[tipo]['texto_evolution'], '') or '').strip()


def renderizar_texto_evolution(tipo, pessoa) -> str:
    nome = (getattr(pessoa, 'nome', '') or '').strip() or 'amigo(a)'
    return texto_evolution_para(tipo).replace('{nome}', nome)


def meta_nome_para(tipo) -> str:
    """Nome do template aprovado na Meta (banco tem prioridade; senao, o .env)."""
    config = _config(tipo)
    if config and (config.meta_nome or '').strip():
        return config.meta_nome.strip()
    return (getattr(settings, _FALLBACK[tipo]['meta_nome'], '') or '').strip()


def meta_idioma_para(tipo) -> str:
    """Codigo de idioma do template na Meta (ex.: pt_BR)."""
    config = _config(tipo)
    if config and (config.meta_idioma or '').strip():
        return config.meta_idioma.strip()
    return (getattr(settings, _FALLBACK[tipo]['meta_idioma'], '') or '').strip() or 'pt_BR'


def parametros_meta_para(tipo, pessoa) -> dict:
    """Variaveis do template ja resolvidas para a pessoa.

    Mesma convencao da Twilio: a variavel "1" recebe o nome da pessoa e `{nome}`
    dentro de qualquer valor e substituido pelo nome.
    """
    nome = (getattr(pessoa, 'nome', '') or '').strip() or 'amigo(a)'
    base = variaveis_dict_para(tipo)
    resolvidas = {
        str(chave): (valor.replace('{nome}', nome) if isinstance(valor, str) else valor)
        for chave, valor in base.items()
    }
    resolvidas['1'] = nome
    return resolvidas


def montar_meta_template(tipo, pessoa) -> dict:
    """Bloco `meta_template` gravado em metadata_envio da mensagem enfileirada."""
    return {
        'nome': meta_nome_para(tipo),
        'idioma': meta_idioma_para(tipo),
        'parametros': parametros_meta_para(tipo, pessoa),
    }


def configurado_para(tipo) -> bool:
    """True quando o provider ativo tem a configuracao minima para enviar."""
    if usando_evolution():
        return bool(texto_evolution_para(tipo))
    if usando_meta():
        return bool(meta_nome_para(tipo))
    return bool(sid_para(tipo))


def mensagem_nao_configurado(tipo) -> str:
    """Texto de erro para a tela quando o template do tipo nao esta configurado."""
    if usando_evolution():
        if tipo == TemplateWhatsapp.Tipo.CONTINUAR:
            return (
                'Mensagem de continuacao do WhatsApp nao configurada. '
                'Defina o texto na tela de configuracao de templates.'
            )
        return (
            'Mensagem de boas-vindas do WhatsApp nao configurada. '
            'Defina o texto na tela de configuracao de templates.'
        )
    if usando_meta():
        if tipo == TemplateWhatsapp.Tipo.CONTINUAR:
            return (
                'Template de continuacao nao configurado. '
                'Defina o nome do template aprovado na Meta na tela de configuracao de templates.'
            )
        return (
            'Template de primeiro contato nao configurado. '
            'Defina o nome do template aprovado na Meta na tela de configuracao de templates.'
        )
    if tipo == TemplateWhatsapp.Tipo.CONTINUAR:
        return 'Template de continuacao nao configurado. Defina o SID na tela de configuracao de templates.'
    return 'Template SID nao configurado. Defina o SID na tela de configuracao de templates.'


def opt_in_sid() -> str:
    return sid_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_variables() -> str:
    return variables_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_texto_evolution() -> str:
    return texto_evolution_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_meta_nome() -> str:
    return meta_nome_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_meta_idioma() -> str:
    return meta_idioma_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def opt_in_configurado() -> bool:
    return configurado_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO)


def continuar_sid() -> str:
    return sid_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_variables() -> str:
    return variables_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_texto_evolution() -> str:
    return texto_evolution_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_meta_nome() -> str:
    return meta_nome_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_meta_idioma() -> str:
    return meta_idioma_para(TemplateWhatsapp.Tipo.CONTINUAR)


def continuar_configurado() -> bool:
    return configurado_para(TemplateWhatsapp.Tipo.CONTINUAR)
