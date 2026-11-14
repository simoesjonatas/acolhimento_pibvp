"""Arquivo anexado a uma opcao do bot (imagem, PDF, audio ou video).

O bot responde com o texto da opcao e, logo depois, com o arquivo. Como a pessoa
acabou de escrever, a janela de 24h esta aberta e a Meta aceita midia sem template.

O arquivo e enviado para a Meta uma vez (POST /media) e o id e reaproveitado por
~25 dias (a Meta guarda por 30); trocar o arquivo zera o id.
"""
from __future__ import annotations

import os
from datetime import timedelta

from django.utils import timezone

from apps.acolhimento import meta_service
from apps.acolhimento.models import MensagemContato, OpcaoAtendimentoBot

# Extensao -> (tipo na Cloud API, mime). So o que a Meta aceita enviar.
EXTENSOES = {
    '.jpg': ('image', 'image/jpeg'),
    '.jpeg': ('image', 'image/jpeg'),
    '.png': ('image', 'image/png'),
    '.mp4': ('video', 'video/mp4'),
    '.3gp': ('video', 'video/3gpp'),
    '.mp3': ('audio', 'audio/mpeg'),
    '.m4a': ('audio', 'audio/mp4'),
    '.aac': ('audio', 'audio/aac'),
    '.amr': ('audio', 'audio/amr'),
    '.ogg': ('audio', 'audio/ogg'),
    '.pdf': ('document', 'application/pdf'),
    '.txt': ('document', 'text/plain'),
    '.doc': ('document', 'application/msword'),
    '.docx': ('document', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    '.xls': ('document', 'application/vnd.ms-excel'),
    '.xlsx': ('document', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    '.ppt': ('document', 'application/vnd.ms-powerpoint'),
    '.pptx': ('document', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
}

# Limites da Cloud API por tipo (documento limitado bem abaixo dos 100 MB da Meta).
LIMITE_MB = {'image': 5, 'video': 16, 'audio': 16, 'document': 25}

MARCADORES = {'image': '(imagem)', 'video': '(video)', 'audio': '(audio)', 'document': '(documento)'}

VALIDADE_ID_META = timedelta(days=25)


def classificar(nome_arquivo: str) -> tuple[str, str] | None:
    """(tipo, mime) pela extensao, ou None quando o formato nao e aceito."""
    _raiz, extensao = os.path.splitext((nome_arquivo or '').lower())
    return EXTENSOES.get(extensao)


def formatos_aceitos() -> str:
    return ', '.join(sorted({ext.lstrip('.') for ext in EXTENSOES}))


def media_id_meta(opcao: OpcaoAtendimentoBot) -> str:
    """Id do arquivo na Meta, enviando (de novo) quando nao ha um valido."""
    agora = timezone.now()
    if (
        opcao.midia_meta_id
        and opcao.midia_meta_enviada_em
        and agora - opcao.midia_meta_enviada_em < VALIDADE_ID_META
    ):
        return opcao.midia_meta_id
    if not opcao.midia_arquivo:
        raise meta_service.MetaWhatsAppError('A opcao do bot nao tem arquivo anexado.')
    with opcao.midia_arquivo.open('rb') as arquivo:
        conteudo = arquivo.read()
    media_id = meta_service.enviar_arquivo_para_meta(
        conteudo,
        mime=opcao.midia_mime,
        nome=opcao.midia_nome_original or os.path.basename(opcao.midia_arquivo.name),
    )
    OpcaoAtendimentoBot.objects.filter(pk=opcao.pk).update(midia_meta_id=media_id, midia_meta_enviada_em=agora)
    opcao.midia_meta_id = media_id
    opcao.midia_meta_enviada_em = agora
    return media_id


def enfileirar_midia(pessoa, opcao: OpcaoAtendimentoBot):
    """Mensagem de saida com o arquivo da opcao (sai depois do texto, na mesma fila)."""
    if not opcao.midia_arquivo or opcao.midia_tipo not in MARCADORES:
        return None
    return MensagemContato.objects.create(
        pessoa=pessoa,
        canal=MensagemContato.CanalChoices.WHATSAPP,
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
        prioridade=5,
        conteudo=MARCADORES[opcao.midia_tipo],
        midia_tipo=opcao.midia_tipo,
        midia_mime=opcao.midia_mime,
        midia_nome_original=opcao.midia_nome_original,
        metadata_envio={'origem': 'bot_atendimento', 'bot_midia_opcao_id': opcao.pk},
    )
