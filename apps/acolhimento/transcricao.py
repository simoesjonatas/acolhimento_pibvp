"""Transcricao dos audios recebidos no WhatsApp.

Ordem: Groq Whisper (API gratis) -> se 429/5xx/sem resposta, faster-whisper no
proprio servidor (`transcricao_local`). Erro de arquivo (400/413) nao vai para a
reserva: o problema e o audio, nao o servico.

Roda depois do download da midia (ja numa thread de fundo ou no cron). Com a
transcricao pronta, o robo responde o audio como se fosse texto, mas so se o
audio ficou esperando por ela (`bot_aguardando_transcricao`) ou se a pessoa
ainda esta com o robo.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from django.utils import timezone

from apps.acolhimento import ia_service, transcricao_local
from apps.acolhimento.models import ConfiguracaoAtendimentoBot, MensagemContato

logger = logging.getLogger(__name__)

Status = MensagemContato.TranscricaoStatusChoices


def transcricao_ligada(cfg=None) -> bool:
    cfg = cfg or ConfiguracaoAtendimentoBot.carregar()
    return bool(cfg.transcrever_audios)


def algum_motor_configurado() -> bool:
    return ia_service.groq_configurada() or transcricao_local.ativa()


def eh_audio_recebido(mensagem: MensagemContato) -> bool:
    return (
        mensagem.direcao == MensagemContato.DirecaoChoices.ENTRADA
        and mensagem.midia_tipo == MensagemContato.MidiaTipoChoices.AUDIO
    )


def campos_iniciais(campos_midia: dict) -> dict:
    """Campos extras na criacao da entrada: audio com transcricao ligada fica 'pendente'."""
    if campos_midia.get('midia_tipo') == MensagemContato.MidiaTipoChoices.AUDIO and transcricao_ligada():
        return {'transcricao_status': Status.PENDENTE}
    return {}


def _registrar(mensagem: MensagemContato, status: str, texto: str = '', info: dict | None = None) -> None:
    mensagem.transcricao = texto
    mensagem.transcricao_status = status
    metadata = dict(mensagem.metadata_resposta or {})
    metadata['transcricao'] = info or {}
    mensagem.metadata_resposta = metadata
    mensagem.save(update_fields=['transcricao', 'transcricao_status', 'metadata_resposta', 'atualizado_em'])


def transcrever_mensagem(mensagem_id: int) -> bool:
    """Transcreve o audio ja baixado. True quando gravou um texto."""
    mensagem = MensagemContato.objects.filter(pk=mensagem_id).first()
    if not mensagem or not eh_audio_recebido(mensagem) or not mensagem.midia_disponivel:
        return False

    inicio = time.monotonic()
    texto = None
    info: dict = {}
    erro_groq = None
    if ia_service.groq_configurada():
        try:
            with mensagem.midia_arquivo.open('rb') as arquivo:
                conteudo = arquivo.read()
            texto = ia_service.transcrever_groq(
                conteudo,
                nome=mensagem.midia_nome_original or mensagem.midia_arquivo.name.rsplit('/', 1)[-1],
                mime=mensagem.midia_mime,
            )
            info = {'motor': 'groq'}
        except ia_service.IAError as exc:
            erro_groq = exc
            logger.warning('Groq nao transcreveu o audio %s (http=%s).', mensagem.pk, exc.http_status)
            if not exc.limite_ou_fora_do_ar:
                _registrar(mensagem, Status.FALHA, info={'motor': 'groq', 'erro': str(exc)[:200]})
                return False

    if texto is None:
        try:
            resultado = transcricao_local.transcrever(mensagem.midia_arquivo.path)
        except Exception as exc:  # noqa: BLE001 - reserva indisponivel ou falhou: registra e segue.
            erro = f'{erro_groq}; reserva local: {exc}' if erro_groq else f'reserva local: {exc}'
            logger.warning('Audio %s sem transcricao: %s', mensagem.pk, erro)
            _registrar(mensagem, Status.FALHA, info={'motor': 'local', 'erro': erro[:200]})
            return False
        texto = resultado['texto']
        info = {'motor': 'local', 'modelo': resultado['modelo'], 'duracao': round(resultado['duracao'], 1)}

    info['segundos'] = round(time.monotonic() - inicio, 1)
    if not texto:
        _registrar(mensagem, Status.FALHA, info={**info, 'erro': 'Nenhuma fala reconhecida.'})
        return False
    _registrar(mensagem, Status.OK, texto=texto, info=info)
    logger.info('Audio %s transcrito por %s em %ss.', mensagem.pk, info['motor'], info['segundos'])
    return True


def _entregar_ao_robo(mensagem: MensagemContato, transcrito: bool) -> None:
    """Depois da transcricao, o robo responde o audio (texto ou, sem texto, o menu)."""
    from apps.acolhimento import atendimento_bot

    mensagem.refresh_from_db()
    aguardando = bool((mensagem.metadata_resposta or {}).get('bot_aguardando_transcricao'))
    pessoa = mensagem.pessoa
    if not atendimento_bot.bot_pode_atender(pessoa):
        return
    # Transcricao atrasada (cron, horas depois): nao responde audio velho.
    if mensagem.enfileirada_em and mensagem.enfileirada_em < timezone.now() - timedelta(hours=1):
        return
    if transcrito:
        atendimento_bot.processar_entrada(pessoa, mensagem.transcricao, mensagem, em_segundo_plano=True)
    elif aguardando:
        # Sem texto nao ha o que a IA entender: o mesmo de antes da IA (saudacao + menu
        # se era a primeira mensagem; senao "nao entendi" + menu).
        cfg = ConfiguracaoAtendimentoBot.carregar()
        opcoes = atendimento_bot.opcoes_ativas()
        if (mensagem.metadata_resposta or {}).get('bot_primeira'):
            atendimento_bot.enfileirar_resposta(pessoa, atendimento_bot._com_menu(cfg.mensagem_saudacao, opcoes))
            atendimento_bot._log(pessoa, 'Atendimento automatico: audio sem transcricao, enviou saudacao + menu.')
        else:
            atendimento_bot.reenviar_menu(pessoa, cfg, opcoes, mensagem.conteudo)


def depois_do_download(mensagem: MensagemContato) -> None:
    """Gancho do download da midia (thread de fundo ou cron)."""
    if not eh_audio_recebido(mensagem) or mensagem.transcricao_status != Status.PENDENTE:
        return
    try:
        transcrito = transcrever_mensagem(mensagem.pk)
        _entregar_ao_robo(mensagem, transcrito)
    except Exception:  # noqa: BLE001 - nunca derruba o download por causa da transcricao.
        logger.exception('Erro inesperado ao transcrever o audio %s.', mensagem.pk)


def transcrever_pendentes(limite: int = 10) -> int:
    """Cron: transcricoes que ficaram para tras (worker reiniciado, midia que falhou)."""
    MensagemContato.objects.filter(
        transcricao_status=Status.PENDENTE,
        midia_status=MensagemContato.MidiaStatusChoices.FALHA,
    ).update(transcricao_status=Status.FALHA)
    folga = timezone.now() - timedelta(minutes=5)
    pendentes = list(
        MensagemContato.objects.filter(
            transcricao_status=Status.PENDENTE,
            midia_status=MensagemContato.MidiaStatusChoices.BAIXADA,
            atualizado_em__lt=folga,
        ).order_by('id')[:limite]
    )
    feitas = 0
    for mensagem in pendentes:
        depois_do_download(mensagem)
        feitas += 1
    return feitas
