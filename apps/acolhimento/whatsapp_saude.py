"""Saude do envio de WhatsApp: corta o disparo automatico quando tudo esta falhando.

Motivacao: uma sequencia de falhas de envio quase nunca e coincidencia — costuma ser
a conta com envio restrito ou o contato em endereçamento LID. Continuar disparando
nesse estado nao entrega nada e ainda queima a reputacao do numero. Entao, em vez de
reenviar (o que piora), desligamos o processamento automatico e avisamos na tela.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.acolhimento.models import ConfiguracaoProcessamentoFila, MensagemContato

logger = logging.getLogger(__name__)

# Quantas saidas consecutivas precisam falhar para o disjuntor abrir.
LIMITE_FALHAS_CONSECUTIVAS = 5

_ESTADOS_TERMINAIS = (
    MensagemContato.StatusFilaChoices.ENVIADA,
    MensagemContato.StatusFilaChoices.FALHA,
)


def _ultimos_desfechos(limite: int) -> list[str]:
    """Status dos ultimos envios que chegaram a um desfecho, do mais recente ao mais antigo.

    Ordena por `atualizado_em` porque uma mensagem pode ser marcada como enviada e so
    depois virar falha, quando o webhook de status chega.
    """
    return list(
        MensagemContato.objects.filter(
            canal=MensagemContato.CanalChoices.WHATSAPP,
            direcao=MensagemContato.DirecaoChoices.SAIDA,
            status_fila__in=_ESTADOS_TERMINAIS,
        )
        .order_by('-atualizado_em', '-id')
        .values_list('status_fila', flat=True)[:limite]
    )


def avaliar_e_pausar_envio(*, limite: int | None = None) -> bool:
    """Desliga o processamento automatico se as ultimas N saidas falharam em sequencia.

    Retorna True somente na transicao (quando ESTA chamada desligou), para o chamador
    poder registrar o evento sem repetir o aviso a cada webhook.
    """
    limite = int(limite or LIMITE_FALHAS_CONSECUTIVAS)
    if limite <= 0:
        return False

    desfechos = _ultimos_desfechos(limite)
    if len(desfechos) < limite:
        return False
    if any(status != MensagemContato.StatusFilaChoices.FALHA for status in desfechos):
        return False

    # `atualizado_em` e auto_now: num UPDATE direto precisamos preencher na mao.
    desligou = ConfiguracaoProcessamentoFila.objects.filter(pk=1, auto_ativo=True).update(
        auto_ativo=False,
        atualizado_em=timezone.now(),
    )
    if not desligou:
        return False

    logger.error(
        'Processamento automatico desligado: %s envios de WhatsApp falharam em sequencia.',
        limite,
    )
    return True


def falhas_recentes(*, horas: int = 24) -> int:
    """Quantas saidas falharam na janela recente (usado pelo aviso no menu)."""
    desde = timezone.now() - timezone.timedelta(hours=horas)
    return MensagemContato.objects.filter(
        canal=MensagemContato.CanalChoices.WHATSAPP,
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        status_fila=MensagemContato.StatusFilaChoices.FALHA,
        atualizado_em__gte=desde,
    ).count()
