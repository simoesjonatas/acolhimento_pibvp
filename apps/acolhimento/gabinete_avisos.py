"""Avisos automaticos quando um gabinete e AGENDADO.

- Pessoa: confirmacao na propria conversa (vai pela fila, como qualquer saida). Com a
  janela de 24h aberta sai como texto livre; fechada, so pelo template aprovado na Meta
  (tipo GABINETE_PESSOA). Sem template e fora da janela, nao enfileira e avisa na tela.
- Pastor: aviso no telefone do perfil dele, enviado direto (nao e conversa com a pessoa,
  entao nao entra na fila dela). Roda numa thread depois do commit para a tela nao
  esperar a Meta. Na Meta, sem template (GABINETE_PASTOR) o texto livre so chega se o
  pastor escreveu para o numero nas ultimas 24h.

O resultado de cada aviso fica em `RegistroGabinete.aviso_pessoa` / `aviso_pastor`.
"""
from __future__ import annotations

import logging
import threading

from django.db import close_old_connections, transaction

from apps.acolhimento import evolution_service, meta_service, template_config
from apps.acolhimento.models import MensagemContato, RegistroGabinete, TemplateWhatsapp
from apps.acolhimento.whatsapp_rules import pode_enviar_livre

logger = logging.getLogger(__name__)

TEXTO_PESSOA_PADRAO = (
    'Olá, {nome}! Seu horário com {pastor} está confirmado para {data} às {hora}. '
    'Se precisar remarcar, é só responder por aqui.'
)
TEXTO_PASTOR_PADRAO = 'Gabinete agendado: {pessoa} em {data} às {hora}.'

# Variaveis padrao dos templates aprovados ({{1}}, {{2}}...). Podem ser trocadas na
# tela de templates (JSON), usando os mesmos marcadores do texto.
VARIAVEIS_PESSOA_PADRAO = {'1': '{nome}', '2': '{data}', '3': '{hora}', '4': '{pastor}'}
VARIAVEIS_PASTOR_PADRAO = {'1': '{pastor}', '2': '{pessoa}', '3': '{data}', '4': '{hora}'}

Tipo = TemplateWhatsapp.Tipo


def _nome_usuario(usuario) -> str:
    if usuario is None:
        return 'o pastor'
    return usuario.get_full_name() or usuario.get_username()


def _marcadores(registro: RegistroGabinete) -> dict[str, str]:
    nome = (registro.pessoa.nome or '').strip() or 'amigo(a)'
    return {
        'nome': nome,
        'pessoa': nome,
        'pastor': _nome_usuario(registro.pastor),
        'data': registro.data.strftime('%d/%m/%Y'),
        'hora': registro.hora.strftime('%H:%M'),
    }


def _aplicar(texto: str, marcadores: dict[str, str]) -> str:
    for chave, valor in marcadores.items():
        texto = texto.replace('{' + chave + '}', valor)
    return texto


def texto_para(tipo) -> str:
    padrao = TEXTO_PESSOA_PADRAO if tipo == Tipo.GABINETE_PESSOA else TEXTO_PASTOR_PADRAO
    return template_config.texto_evolution_para(tipo) or padrao


def parametros_para(tipo, marcadores: dict[str, str]) -> dict[str, str]:
    padrao = VARIAVEIS_PESSOA_PADRAO if tipo == Tipo.GABINETE_PESSOA else VARIAVEIS_PASTOR_PADRAO
    variaveis = template_config.variaveis_dict_para(tipo) or padrao
    return {str(chave): _aplicar(str(valor), marcadores) for chave, valor in variaveis.items()}


def _template_meta(tipo, marcadores) -> dict:
    return {
        'nome': template_config.meta_nome_para(tipo),
        'idioma': template_config.meta_idioma_para(tipo),
        'parametros': parametros_para(tipo, marcadores),
    }


# ---------------------------------------------------------------------------
# Pessoa
# ---------------------------------------------------------------------------
def _avisar_pessoa(registro: RegistroGabinete, usuario) -> str:
    pessoa = registro.pessoa
    if not (pessoa.telefone_whatsapp or '').strip():
        return 'Pessoa sem WhatsApp cadastrado: confirmação não enviada.'

    marcadores = _marcadores(registro)
    texto = _aplicar(texto_para(Tipo.GABINETE_PESSOA), marcadores)
    metadata = {'origem': 'gabinete_agendado', 'gabinete_id': registro.pk}
    resultado = 'Confirmação enfileirada para a pessoa.'

    if template_config.usando_meta() and not pode_enviar_livre(pessoa):
        if not template_config.meta_nome_para(Tipo.GABINETE_PESSOA):
            return (
                'Confirmação não enviada: a pessoa está fora da janela de 24h e o template '
                '"Gabinete agendado (para a pessoa)" não está configurado.'
            )
        metadata['tipo_template'] = Tipo.GABINETE_PESSOA
        metadata['meta_template'] = _template_meta(Tipo.GABINETE_PESSOA, marcadores)
        resultado = 'Confirmação enfileirada para a pessoa (template da Meta).'

    MensagemContato.objects.create(
        pessoa=pessoa,
        criado_por=usuario,
        canal=MensagemContato.CanalChoices.WHATSAPP,
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
        prioridade=3,
        conteudo=texto,
        metadata_envio=metadata,
    )
    return resultado


# ---------------------------------------------------------------------------
# Pastor
# ---------------------------------------------------------------------------
def _telefone_pastor(pastor) -> str:
    from apps.core.models import telefone_do_usuario

    return telefone_do_usuario(pastor) if pastor else ''


def enviar_aviso_pastor(registro_id: int) -> str:
    """Envia o aviso ao pastor agora e grava o resultado. Retorna o texto gravado."""
    from apps.acolhimento.fila_processor import normalize_phone_br

    registro = RegistroGabinete.objects.select_related('pessoa', 'pastor').filter(pk=registro_id).first()
    if registro is None:
        return ''
    marcadores = _marcadores(registro)
    try:
        destino = normalize_phone_br(_telefone_pastor(registro.pastor))
        if template_config.usando_meta():
            if template_config.meta_nome_para(Tipo.GABINETE_PASTOR):
                template = _template_meta(Tipo.GABINETE_PASTOR, marcadores)
                meta_service.send_template(
                    to_phone=destino,
                    nome=template['nome'],
                    idioma=template['idioma'],
                    parametros=template['parametros'],
                )
            else:
                meta_service.send_text(to_phone=destino, text=_aplicar(texto_para(Tipo.GABINETE_PASTOR), marcadores))
        elif template_config.usando_evolution():
            evolution_service.send_whatsapp_text(
                to_phone=destino, text=_aplicar(texto_para(Tipo.GABINETE_PASTOR), marcadores),
            )
        else:
            raise RuntimeError('aviso ao pastor so pela API da Meta ou pelo Evolution')
        resultado = 'Aviso enviado ao pastor.'
    except Exception as exc:  # noqa: BLE001 - qualquer falha vira texto na tela, nunca 500.
        resultado = f'Aviso ao pastor falhou: {exc}'[:255]
        logger.warning('Gabinete %s: aviso ao pastor falhou.', registro_id, exc_info=True)

    RegistroGabinete.objects.filter(pk=registro_id).update(aviso_pastor=resultado)
    return resultado


def _enviar_aviso_pastor_em_thread(registro_id: int) -> None:
    try:
        enviar_aviso_pastor(registro_id)
    finally:
        close_old_connections()


def _agendar_aviso_pastor(registro: RegistroGabinete) -> str:
    if registro.pastor is None:
        return 'Sem pastor definido: ninguém foi avisado.'
    if not _telefone_pastor(registro.pastor):
        return 'Pastor sem telefone no perfil: aviso não enviado.'
    transaction.on_commit(
        lambda: threading.Thread(
            target=_enviar_aviso_pastor_em_thread, args=(registro.pk,), daemon=True,
        ).start()
    )
    return 'Aviso ao pastor sendo enviado.'


def avisar_agendamento(registro: RegistroGabinete, usuario=None) -> None:
    """Dispara os dois avisos de um gabinete agendado. Nunca levanta excecao."""
    if registro.tipo != RegistroGabinete.Tipo.AGENDADO:
        return
    try:
        registro.aviso_pessoa = _avisar_pessoa(registro, usuario)[:255]
    except Exception:  # noqa: BLE001
        logger.exception('Gabinete %s: falha ao enfileirar a confirmacao da pessoa.', registro.pk)
        registro.aviso_pessoa = 'Falha ao enfileirar a confirmação para a pessoa.'
    registro.aviso_pastor = _agendar_aviso_pastor(registro)[:255]
    # Atualiza so os avisos: a thread do pastor pode gravar o resultado final depois.
    RegistroGabinete.objects.filter(pk=registro.pk).update(
        aviso_pessoa=registro.aviso_pessoa,
        aviso_pastor=registro.aviso_pastor,
    )
