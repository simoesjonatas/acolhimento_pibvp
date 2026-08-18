"""Processamento da fila de mensagens (runner + modo automatico event-driven).

Este modulo concentra a "maquina" de execucao da fila:
  - `executar_fila_execucao`: roda UMA execucao (`ExecucaoProcessamentoFila`) do
    inicio ao fim e finaliza o registro. Compartilhado pela thread (modo
    automatico/manual) e pelo comando de cron `processar_fila`.
  - `iniciar_execucao_fila`: cria a execucao e dispara a thread daemon.
  - `disparar_auto_se_ligado`: modo automatico event-driven (signal ao enfileirar).
  - `recuperar_execucoes_presas`: destrava execucoes orfas (processo morreu no meio).

Concorrencia:
  - Um `Lock` em processo + a checagem "existe execucao EXECUTANDO?" garantem no
    maximo uma execucao por vez NESTE processo.
  - Entre PROCESSOS diferentes (workers do Gunicorn + o cron `processar_fila`), a
    seguranca contra envio duplicado vem do CLAIM ATOMICO em `fila_processor`
    (`_reivindicar_mensagem`): cada mensagem so e assumida por um processador.

Antes o runner vivia em `views.py` e era importado de forma tardia aqui (ciclo de
import). Agora vive neste modulo; `views.py` apenas chama `iniciar_execucao_fila`.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.acolhimento.fila_processor import processar_fila_mensagens
from apps.acolhimento.models import (
    ConfiguracaoProcessamentoFila,
    ExecucaoProcessamentoFila,
    MensagemContato,
)

logger = logging.getLogger(__name__)

# Serializa a criacao de execucoes entre threads deste processo.
_start_lock = threading.Lock()

# Limite por rodada no modo automatico (o encadeamento drena o resto).
LIMITE_AUTO = 50

# Tempo sem progresso (atualizado_em) apos o qual consideramos uma execucao
# "presa" (ex.: o processo morreu no meio) e liberamos a fila.
TIMEOUT_EXECUCAO_PRESA_MIN = 15


def ha_execucao_ativa() -> bool:
    return ExecucaoProcessamentoFila.objects.filter(
        status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO
    ).exists()


def ha_pendentes_saida() -> bool:
    return MensagemContato.objects.filter(
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        canal=MensagemContato.CanalChoices.WHATSAPP,
        status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
    ).exists()


def _append_execucao_log(execucao: ExecucaoProcessamentoFila, texto: str) -> None:
    linha = f'[{timezone.now().strftime("%d/%m/%Y %H:%M:%S")}] {texto}'
    execucao.log_execucao = f'{execucao.log_execucao}\n{linha}'.strip()
    execucao.save(update_fields=['log_execucao', 'atualizado_em'])


def recuperar_execucoes_presas(timeout_minutos: int = TIMEOUT_EXECUCAO_PRESA_MIN) -> int:
    """Marca como INTERROMPIDA execucoes travadas em EXECUTANDO sem progresso.

    Uma execucao real atualiza `atualizado_em` a cada mensagem processada; se ela
    ficou parada alem do timeout, o processo que a rodava provavelmente morreu
    (deploy/restart no meio). Sem isso, `ha_execucao_ativa()` bloquearia todas as
    proximas execucoes indefinidamente.
    """
    limite = timezone.now() - timedelta(minutes=timeout_minutos)
    presas = list(
        ExecucaoProcessamentoFila.objects.filter(
            status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO,
            atualizado_em__lt=limite,
        )
    )
    for execucao in presas:
        execucao.status = ExecucaoProcessamentoFila.StatusExecucaoChoices.INTERROMPIDA
        execucao.finalizado_em = timezone.now()
        linha = (
            f'[{timezone.now().strftime("%d/%m/%Y %H:%M:%S")}] Execucao interrompida: '
            f'sem progresso ha mais de {timeout_minutos} min (provavel reinicio do processo).'
        )
        execucao.log_execucao = f'{execucao.log_execucao}\n{linha}'.strip()
        execucao.save(update_fields=['status', 'finalizado_em', 'log_execucao', 'atualizado_em'])
    if presas:
        logger.warning('Recuperadas %s execucao(oes) de fila presas em EXECUTANDO.', len(presas))
    return len(presas)


def executar_fila_execucao(execucao_id: int, *, encadear_auto: bool = True) -> None:
    """Roda uma execucao da fila do inicio ao fim e finaliza o registro.

    Compartilhado por:
      - a thread do modo automatico/manual (`encadear_auto=True`: encadeia o
        proximo lote se sobrar pendente e o switch estiver ligado);
      - o comando de cron `processar_fila` (`encadear_auto=False`: roda sincrono
        no proprio processo e nao dispara threads).
    """
    close_old_connections()
    execucao = ExecucaoProcessamentoFila.objects.get(pk=execucao_id)

    def _progress(texto: str) -> None:
        execucao.refresh_from_db(fields=['id', 'log_execucao', 'atualizado_em'])
        _append_execucao_log(execucao, texto)

    def _should_stop() -> bool:
        execucao.refresh_from_db(fields=['solicitar_parada'])
        return bool(execucao.solicitar_parada)

    try:
        resultado = processar_fila_mensagens(
            limit=execucao.limite,
            ids=[int(i) for i in execucao.ids_filtrados if str(i).isdigit()],
            dry_run=execucao.dry_run,
            progress_callback=_progress,
            should_stop=_should_stop,
        )

        execucao.refresh_from_db()
        execucao.total_selecionado = resultado['total_selecionado']
        execucao.total_processado = resultado['total_processado']
        execucao.total_sucesso = resultado['sucesso']
        execucao.total_falha = resultado['falha']
        execucao.finalizado_em = timezone.now()
        execucao.status = (
            ExecucaoProcessamentoFila.StatusExecucaoChoices.INTERROMPIDA
            if resultado['interrompido']
            else ExecucaoProcessamentoFila.StatusExecucaoChoices.CONCLUIDA
        )
        execucao.save(
            update_fields=[
                'total_selecionado',
                'total_processado',
                'total_sucesso',
                'total_falha',
                'finalizado_em',
                'status',
                'atualizado_em',
            ]
        )

        # Modo automatico (thread): se o switch estiver ligado e sobrou pendente,
        # encadeia outra execucao para drenar o resto.
        if encadear_auto and execucao.status == ExecucaoProcessamentoFila.StatusExecucaoChoices.CONCLUIDA:
            disparar_auto_se_ligado()
    except Exception as exc:
        logger.exception('Falha inesperada ao processar a fila (execucao=%s).', execucao_id)
        execucao.refresh_from_db()
        _append_execucao_log(execucao, f'Erro inesperado: {exc}')
        execucao.status = ExecucaoProcessamentoFila.StatusExecucaoChoices.FALHA
        execucao.finalizado_em = timezone.now()
        execucao.save(update_fields=['status', 'finalizado_em', 'atualizado_em'])
    finally:
        close_old_connections()


def iniciar_execucao_fila(
    *,
    solicitado_por=None,
    limite: int = 20,
    dry_run: bool = False,
    ids=None,
    origem: str = ExecucaoProcessamentoFila.OrigemChoices.MANUAL,
) -> ExecucaoProcessamentoFila | None:
    """Cria uma execucao e dispara a thread. Retorna None se ja houver uma rodando."""
    with _start_lock:
        if ha_execucao_ativa():
            # A "ativa" pode ser uma execucao presa de um processo que morreu:
            # tenta destravar antes de desistir.
            recuperar_execucoes_presas()
            if ha_execucao_ativa():
                return None
        execucao = ExecucaoProcessamentoFila.objects.create(
            solicitado_por=solicitado_por,
            status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO,
            limite=limite,
            dry_run=dry_run,
            ids_filtrados=list(ids or []),
            origem=origem,
        )

    threading.Thread(target=executar_fila_execucao, args=(execucao.id,), daemon=True).start()
    return execucao


def disparar_auto_se_ligado(*, limite: int = LIMITE_AUTO) -> ExecucaoProcessamentoFila | None:
    """Inicia uma execucao automatica se: switch ligado, ha pendentes e nada rodando."""
    if not ConfiguracaoProcessamentoFila.auto_ligado():
        return None
    if not ha_pendentes_saida():
        return None
    return iniciar_execucao_fila(
        solicitado_por=None,
        limite=limite,
        origem=ExecucaoProcessamentoFila.OrigemChoices.AUTOMATICO,
    )


def agendar_disparo_auto() -> None:
    """Agenda o disparo automatico para rodar depois do commit da transacao atual.

    Usado nos pontos de enfileiramento que usam bulk_create (que nao emite o
    signal post_save). Fora de uma transacao, o Django executa na hora.
    """
    transaction.on_commit(disparar_auto_se_ligado)
