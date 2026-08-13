"""Signals do app acolhimento.

Processamento automatico da fila: toda vez que uma mensagem de saida e enfileirada
(criada como PENDENTE), agenda o consumo da fila. O disparo so acontece de fato se
o switch (ConfiguracaoProcessamentoFila) estiver ligado — ver `fila_auto`.
"""
from __future__ import annotations

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.acolhimento.models import MensagemContato


@receiver(post_save, sender=MensagemContato, dispatch_uid='acolhimento_disparo_auto_fila')
def _disparar_auto_ao_enfileirar(sender, instance, created, **kwargs):
    # Apenas mensagens de saida recem-criadas e pendentes contam como "evento de envio".
    # (bulk_create nao emite este signal; esses pontos chamam agendar_disparo_auto()
    # explicitamente.)
    if not created:
        return
    if instance.direcao != MensagemContato.DirecaoChoices.SAIDA:
        return
    if instance.canal != MensagemContato.CanalChoices.WHATSAPP:
        return
    if instance.status_fila != MensagemContato.StatusFilaChoices.PENDENTE:
        return

    from apps.acolhimento import fila_auto
    fila_auto.agendar_disparo_auto()
