"""Processamento automatico da fila de mensagens (event-driven).

Reutiliza a mesma maquina do processamento manual (`ExecucaoProcessamentoFila`
+ thread daemon `_run_execucao_fila`). Um switch persistente
(`ConfiguracaoProcessamentoFila`) liga/desliga o modo automatico.

Fluxo do modo automatico:
  alguem enfileira uma mensagem de saida  ->  signal post_save (signals.py)
  ->  disparar_auto_se_ligado()  ->  se o switch estiver ligado e houver
  pendentes e nenhuma execucao rodando, inicia uma execucao numa thread.

Guarda de concorrencia: um Lock em processo + a checagem "existe execucao
EXECUTANDO?" garantem no maximo uma execucao por vez (evita envio duplicado).
"""
from __future__ import annotations

import threading

from django.db import transaction

from apps.acolhimento.models import (
    ConfiguracaoProcessamentoFila,
    ExecucaoProcessamentoFila,
    MensagemContato,
)

# Serializa a criacao de execucoes entre threads deste processo.
_start_lock = threading.Lock()

# Limite por rodada no modo automatico (o encadeamento drena o resto).
LIMITE_AUTO = 50


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


def iniciar_execucao_fila(
    *,
    solicitado_por=None,
    limite: int = 20,
    dry_run: bool = False,
    ids=None,
    origem: str = ExecucaoProcessamentoFila.OrigemChoices.MANUAL,
) -> ExecucaoProcessamentoFila | None:
    """Cria uma execucao e dispara a thread. Retorna None se ja houver uma rodando."""
    # Import tardio: _run_execucao_fila vive em views.py (evita ciclo de import).
    from apps.acolhimento.views import _run_execucao_fila

    with _start_lock:
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

    threading.Thread(target=_run_execucao_fila, args=(execucao.id,), daemon=True).start()
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
