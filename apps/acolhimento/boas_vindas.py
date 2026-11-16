"""Boas-vindas (template de primeiro contato): disparo manual e automatico.

- Manual: o botao "Disparar boas-vindas" do painel enfileira para todo mundo elegivel.
- Automatico: com o switch ligado (`ConfiguracaoProcessamentoFila.boas_vindas_auto`),
  cada pessoa cadastrada pela equipe ou pelo auto-cadastro ja recebe na hora.

Elegivel = status Primeiro contato, nao escreveu antes para o numero (quem escreve
primeiro ja e atendido pelo robo) e passou da carencia HORAS_ESPERA_OPTIN, se houver.
Quem recebe vira Visitante (status ROBO), como no disparo manual.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.acolhimento import fila_auto, template_config
from apps.acolhimento.models import (
    ConfiguracaoProcessamentoFila,
    MensagemContato,
    PrimeiroContato,
    TemplateWhatsapp,
)

logger = logging.getLogger(__name__)

TIPO = TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO


def elegiveis():
    queryset = PrimeiroContato.objects.filter(
        status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
    ).exclude(iniciou_interacao=True)
    horas_espera = int(getattr(settings, 'HORAS_ESPERA_OPTIN', 0) or 0)
    if horas_espera > 0:
        queryset = queryset.filter(criado_em__lte=timezone.now() - timedelta(hours=horas_espera))
    return queryset


def automatico_ligado() -> bool:
    return ConfiguracaoProcessamentoFila.objects.filter(pk=1, boas_vindas_auto=True).exists()


def _mensagem(pessoa, usuario, *, template_sid, template_vars_base, texto_evolution_base) -> MensagemContato:
    usa_meta = template_config.usando_meta()
    usa_evolution = template_config.usando_evolution()
    nome = (pessoa.nome or '').strip() or 'amigo(a)'
    metadata_envio = {'tipo_template': TIPO}
    if template_sid:
        metadata_envio['twilio_template'] = {
            'content_sid': template_sid,
            'content_variables': json.dumps({**template_vars_base, '1': nome}, ensure_ascii=False),
        }
    if usa_meta:
        metadata_envio['meta_template'] = template_config.montar_meta_template(TIPO, pessoa)
    if usa_evolution:
        conteudo = texto_evolution_base.replace('{nome}', nome)
        metadata_envio['evolution_texto'] = conteudo
    elif usa_meta:
        # Grava o texto que a pessoa vai ler (com o nome dela), nao so o nome do template.
        conteudo = template_config.conteudo_template(metadata_envio['meta_template'], 'Template opt-in enfileirado')
    else:
        conteudo = 'Template opt-in enfileirado'
    return MensagemContato(
        pessoa=pessoa,
        criado_por=usuario,
        canal=MensagemContato.CanalChoices.WHATSAPP,
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
        prioridade=5,
        conteudo=conteudo,
        metadata_envio=metadata_envio,
    )


def enfileirar(pessoas, usuario=None) -> list[MensagemContato]:
    """Enfileira as boas-vindas para `pessoas` e passa todas para Visitante.

    Quem chama confere antes se o template esta configurado (`configurado()`).
    """
    pessoas = list(pessoas)
    if not pessoas:
        return []
    template_sid = '' if template_config.usando_meta() else template_config.opt_in_sid()
    try:
        template_vars_base = json.loads(template_config.opt_in_variables() or '{}')
        if not isinstance(template_vars_base, dict):
            template_vars_base = {}
    except Exception:
        template_vars_base = {}
    texto_evolution_base = template_config.opt_in_texto_evolution() if template_config.usando_evolution() else ''

    mensagens = [
        _mensagem(
            pessoa, usuario,
            template_sid=template_sid,
            template_vars_base=template_vars_base,
            texto_evolution_base=texto_evolution_base,
        )
        for pessoa in pessoas
    ]
    # Espaca os envios no tempo (anti-bloqueio; no modo Meta nao escalona).
    fila_auto.escalonar_agendamento(mensagens)
    MensagemContato.objects.bulk_create(mensagens)
    PrimeiroContato.objects.filter(id__in=[pessoa.id for pessoa in pessoas]).update(
        status=PrimeiroContato.StatusAcolhimento.ROBO,
    )
    # bulk_create nao emite signal: dispara o processamento automatico aqui.
    fila_auto.agendar_disparo_auto()
    return mensagens


def configurado() -> bool:
    return template_config.opt_in_configurado()


def ao_cadastrar(pessoa, usuario=None) -> bool:
    """Chamado logo depois de cadastrar uma pessoa. True se as boas-vindas foram enfileiradas.

    Nao faz nada com o switch desligado, sem WhatsApp, fora dos criterios de
    elegibilidade ou sem template configurado (este ultimo fica no log).
    """
    if not automatico_ligado():
        return False
    if not (pessoa.telefone_whatsapp or '').strip():
        return False
    if not elegiveis().filter(pk=pessoa.pk).exists():
        return False
    if not configurado():
        logger.warning('Boas-vindas automaticas ligadas, mas o template de primeiro contato nao esta configurado.')
        return False
    try:
        enfileirar([pessoa], usuario)
    except Exception:
        # Nao derruba o cadastro: a pessoa continua em Primeiro contato e pode ir no manual.
        logger.exception('Falha ao enfileirar boas-vindas automaticas (pessoa %s).', pessoa.pk)
        return False
    pessoa.status = PrimeiroContato.StatusAcolhimento.ROBO
    return True
