"""Midia recebida pelo WhatsApp (API oficial da Meta): extracao, download e retentativa.

Fluxo:
  1. o webhook grava a entrada com `midia_id` e `midia_status=pendente` (sem rede);
  2. depois do commit, `agendar_download` dispara uma thread curta que baixa o
     arquivo (`baixar_midia_da_mensagem`), com algumas tentativas rapidas;
  3. o que sobrar pendente (Meta fora do ar, processo reiniciado) e retentado pelo
     cron `processar_fila` via `baixar_midias_pendentes`.

A mensagem de texto ja esta salva antes de qualquer download: falha aqui so deixa
a midia como "indisponivel" na conversa. O arquivo vai para `MIDIA_PRIVADA_ROOT`
(sem URL publica) e so sai pela view autenticada `MidiaMensagemView`.
"""
from __future__ import annotations

import logging
import mimetypes
import threading
import time
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.acolhimento import meta_service
from apps.acolhimento.models import MensagemContato

logger = logging.getLogger(__name__)

TIPOS_MIDIA = {choice for choice, _rotulo in MensagemContato.MidiaTipoChoices.choices}

# Tentativas no total (thread + cron) antes de desistir de vez.
MAX_TENTATIVAS = 5
# Pausas entre as tentativas rapidas da thread logo apos o webhook.
ESPERAS_THREAD_SEGUNDOS = (0, 3, 10)
# A Meta guarda a midia por ~30 dias; depois disso o id nao serve mais.
VALIDADE_MIDIA_META = timedelta(days=29)

_EXTENSOES_PADRAO = {
    'audio/ogg': '.ogg',
    'audio/mpeg': '.mp3',
    'audio/mp4': '.m4a',
    'audio/aac': '.aac',
    'audio/amr': '.amr',
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'video/mp4': '.mp4',
    'video/3gpp': '.3gp',
    'application/pdf': '.pdf',
}


def extrair_midia(message: dict) -> dict | None:
    """Dados da midia de uma mensagem do webhook. None quando nao ha midia."""
    tipo = str(message.get('type') or '')
    if tipo not in TIPOS_MIDIA:
        return None
    bloco = message.get(tipo) or {}
    if not isinstance(bloco, dict):
        return None
    media_id = str(bloco.get('id') or '').strip()
    if not media_id:
        return None
    return {
        'tipo': tipo,
        'media_id': media_id,
        'mime_type': str(bloco.get('mime_type') or '').strip(),
        'filename': str(bloco.get('filename') or '').strip(),
        'sha256': str(bloco.get('sha256') or '').strip(),
    }


def campos_midia_entrada(message: dict) -> dict:
    """Campos de `MensagemContato` para gravar junto da entrada (sem rede)."""
    midia = extrair_midia(message)
    if not midia:
        return {}
    return {
        'midia_tipo': midia['tipo'],
        'midia_id': midia['media_id'],
        'midia_mime': midia['mime_type'][:120],
        'midia_nome_original': midia['filename'][:255],
        'midia_status': MensagemContato.MidiaStatusChoices.PENDENTE,
    }


def _extensao(mime: str, nome_original: str) -> str:
    mime_base = (mime or '').split(';')[0].strip().lower()
    if mime_base in _EXTENSOES_PADRAO:
        return _EXTENSOES_PADRAO[mime_base]
    extensao = mimetypes.guess_extension(mime_base) if mime_base else None
    if extensao:
        return extensao
    if '.' in (nome_original or ''):
        return '.' + nome_original.rsplit('.', 1)[-1][:8]
    return '.bin'


def _registrar_falha(mensagem: MensagemContato, erro: str, *, definitiva: bool) -> None:
    mensagem.midia_tentativas += 1
    mensagem.midia_erro = erro[:255]
    if definitiva or mensagem.midia_tentativas >= MAX_TENTATIVAS:
        mensagem.midia_status = MensagemContato.MidiaStatusChoices.FALHA
    mensagem.save(update_fields=['midia_tentativas', 'midia_erro', 'midia_status', 'atualizado_em'])


def baixar_midia_da_mensagem(mensagem_id: int) -> bool:
    """Uma tentativa de baixar a midia da mensagem. True quando o arquivo ficou salvo."""
    mensagem = MensagemContato.objects.filter(pk=mensagem_id).first()
    if not mensagem or mensagem.midia_status != MensagemContato.MidiaStatusChoices.PENDENTE:
        return bool(mensagem and mensagem.midia_disponivel)
    if not mensagem.midia_id:
        _registrar_falha(mensagem, 'Mensagem sem id de midia.', definitiva=True)
        return False

    limite = meta_service.limite_midia_bytes()
    try:
        info = meta_service.obter_url_midia(mensagem.midia_id)
        tamanho_meta = int(info.get('file_size') or 0)
        if tamanho_meta > limite:
            raise meta_service.MidiaGrandeDemaisError(
                f'Midia com {tamanho_meta} bytes passa do limite de {limite}.'
            )
        conteudo = meta_service.baixar_midia(str(info['url']), limite_bytes=limite)
    except meta_service.MidiaGrandeDemaisError as exc:
        _registrar_falha(mensagem, str(exc), definitiva=True)
        logger.warning('Midia da mensagem %s nao baixada: maior que o limite.', mensagem.pk)
        return False
    except Exception as exc:  # noqa: BLE001 - qualquer falha vira retentativa, nunca 500.
        _registrar_falha(mensagem, str(exc) or exc.__class__.__name__, definitiva=False)
        logger.warning('Falha ao baixar midia da mensagem %s (tentativa %s).', mensagem.pk, mensagem.midia_tentativas)
        return False

    mime = str(info.get('mime_type') or mensagem.midia_mime or '').strip()[:120]
    nome_disco = f'midia{_extensao(mime, mensagem.midia_nome_original)}'
    mensagem.midia_arquivo.save(nome_disco, ContentFile(conteudo), save=False)
    mensagem.midia_mime = mime
    mensagem.midia_tamanho = len(conteudo)
    mensagem.midia_status = MensagemContato.MidiaStatusChoices.BAIXADA
    mensagem.midia_tentativas += 1
    mensagem.midia_erro = ''
    mensagem.save(update_fields=[
        'midia_arquivo', 'midia_mime', 'midia_tamanho', 'midia_status',
        'midia_tentativas', 'midia_erro', 'atualizado_em',
    ])
    logger.info('Midia da mensagem %s baixada (%s bytes).', mensagem.pk, len(conteudo))
    return True


def _baixar_com_retentativa(mensagem_id: int) -> None:
    try:
        for espera in ESPERAS_THREAD_SEGUNDOS:
            if espera:
                time.sleep(espera)
            if baixar_midia_da_mensagem(mensagem_id):
                return
            situacao = MensagemContato.objects.filter(pk=mensagem_id).values_list('midia_status', flat=True).first()
            if situacao != MensagemContato.MidiaStatusChoices.PENDENTE:
                return
    except Exception:  # noqa: BLE001 - thread de fundo nao pode morrer com traceback solto.
        logger.exception('Erro inesperado no download da midia da mensagem %s.', mensagem_id)
    finally:
        close_old_connections()


def agendar_download(mensagem_id: int) -> None:
    """Baixa a midia em segundo plano DEPOIS do commit (o webhook responde antes)."""
    transaction.on_commit(
        lambda: threading.Thread(target=_baixar_com_retentativa, args=(mensagem_id,), daemon=True).start()
    )


def baixar_midias_pendentes(limite: int = 20) -> int:
    """Retentativa pelo cron: baixa as pendentes que a thread nao conseguiu. Retorna quantas baixou."""
    corte = timezone.now() - VALIDADE_MIDIA_META
    vencidas = MensagemContato.objects.filter(
        midia_status=MensagemContato.MidiaStatusChoices.PENDENTE,
        enfileirada_em__lt=corte,
    )
    vencidas.update(midia_status=MensagemContato.MidiaStatusChoices.FALHA, midia_erro='Midia expirou na Meta.')

    # Folga para nao disputar com a thread que acabou de ser disparada pelo webhook.
    recentes = timezone.now() - timedelta(minutes=2)
    ids = list(
        MensagemContato.objects.filter(
            midia_status=MensagemContato.MidiaStatusChoices.PENDENTE,
            midia_tentativas__lt=MAX_TENTATIVAS,
            enfileirada_em__lt=recentes,
        )
        .order_by('enfileirada_em', 'id')
        .values_list('id', flat=True)[:limite]
    )
    return sum(1 for mensagem_id in ids if baixar_midia_da_mensagem(mensagem_id))
