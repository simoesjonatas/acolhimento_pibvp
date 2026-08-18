"""Metricas de entrega das mensagens de saida (WhatsApp).

Consolida indicadores simples para a tela de processamento da fila:
taxa de sucesso, tempo medio na fila e mensagens "presas" em processamento.

Volume deste sistema e baixo (acolhimento de igreja), entao o tempo medio e
calculado em Python sobre a janela — evita aritmetica de DurationField, que tem
comportamento diferente entre SQLite (dev/testes) e Postgres (producao).
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.acolhimento.models import MensagemContato


def _fmt_duracao(segundos: int | None) -> str | None:
    if segundos is None:
        return None
    if segundos < 60:
        return f'{segundos}s'
    minutos, seg = divmod(segundos, 60)
    if minutos < 60:
        return f'{minutos}min {seg}s'
    horas, minutos = divmod(minutos, 60)
    return f'{horas}h {minutos}min'


def metricas_entrega(*, dias: int = 7, presas_minutos: int = 30) -> dict:
    """Resumo de entrega das saidas de WhatsApp nos ultimos `dias`.

    - taxa_sucesso: enviadas / (enviadas + falhas) * 100 (None se nao houve nenhuma).
    - tempo_medio_seg: media de (enviada_em - enfileirada_em) das enviadas na janela.
    - presas: mensagens em PROCESSANDO ha mais de `presas_minutos` (envio travado).
    """
    agora = timezone.now()
    desde = agora - timedelta(days=dias)

    saida = MensagemContato.objects.filter(
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        canal=MensagemContato.CanalChoices.WHATSAPP,
    )
    janela = saida.filter(enfileirada_em__gte=desde)

    contagem = janela.aggregate(
        enviadas=Count('id', filter=Q(status_fila=MensagemContato.StatusFilaChoices.ENVIADA)),
        falhas=Count('id', filter=Q(status_fila=MensagemContato.StatusFilaChoices.FALHA)),
        canceladas=Count('id', filter=Q(status_fila=MensagemContato.StatusFilaChoices.CANCELADA)),
    )
    enviadas = contagem['enviadas'] or 0
    falhas = contagem['falhas'] or 0
    total_final = enviadas + falhas
    taxa_sucesso = round(enviadas / total_final * 100, 1) if total_final else None

    duracoes = [
        (env - enf).total_seconds()
        for enf, env in janela.filter(
            status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
            enviada_em__isnull=False,
        ).values_list('enfileirada_em', 'enviada_em')
        if enf and env
    ]
    tempo_medio_seg = int(sum(duracoes) / len(duracoes)) if duracoes else None

    limite_presa = agora - timedelta(minutes=presas_minutos)
    presas = saida.filter(
        status_fila=MensagemContato.StatusFilaChoices.PROCESSANDO,
        atualizado_em__lt=limite_presa,
    ).count()

    return {
        'dias': dias,
        'enviadas': enviadas,
        'falhas': falhas,
        'canceladas': contagem['canceladas'] or 0,
        'taxa_sucesso': taxa_sucesso,
        'tempo_medio_seg': tempo_medio_seg,
        'tempo_medio_texto': _fmt_duracao(tempo_medio_seg),
        'presas': presas,
        'presas_minutos': presas_minutos,
    }
