"""Reserva de transcricao: faster-whisper rodando no proprio servidor (CPU).

Usada quando a Groq devolve 429 (limite do plano gratis) ou esta fora do ar.
- O modelo e carregado na primeira vez e fica em memoria no worker.
- Um `flock` garante uma transcricao local por vez no servidor inteiro (os 3
  workers do gunicorn), para nao travar a CPU do site.
- O import do faster_whisper e preguicoso: sem o pacote, a reserva so avisa.
"""
from __future__ import annotations

import fcntl
import logging
import threading
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_modelo = None
_modelo_nome = ''
_carga_lock = threading.Lock()


class TranscricaoLocalIndisponivel(RuntimeError):
    """Reserva desligada, sem o pacote instalado ou audio longo demais."""


def ativa() -> bool:
    return bool(getattr(settings, 'TRANSCRICAO_LOCAL_ATIVA', False))


def disponivel() -> bool:
    if not ativa():
        return False
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def _pasta_modelos() -> Path:
    pasta = Path(getattr(settings, 'WHISPER_MODELOS_DIR', '') or 'data/whisper')
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def carregar_modelo():
    """WhisperModel em int8 no CPU (baixa os pesos na primeira vez para WHISPER_MODELOS_DIR)."""
    global _modelo, _modelo_nome
    nome = (getattr(settings, 'TRANSCRICAO_LOCAL_MODELO', '') or 'small').strip()
    with _carga_lock:
        if _modelo is not None and _modelo_nome == nome:
            return _modelo
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscricaoLocalIndisponivel('faster-whisper nao esta instalado no servidor.') from exc
        _modelo = WhisperModel(
            nome,
            device='cpu',
            compute_type='int8',
            cpu_threads=2,
            download_root=str(_pasta_modelos()),
        )
        _modelo_nome = nome
        return _modelo


def transcrever(caminho: str) -> dict:
    """Transcreve um arquivo de audio. Devolve {texto, modelo, duracao}.

    Levanta TranscricaoLocalIndisponivel quando a reserva esta desligada, falta o
    pacote ou o audio passa de TRANSCRICAO_LOCAL_MAX_SEGUNDOS.
    """
    if not ativa():
        raise TranscricaoLocalIndisponivel('Transcricao local desligada (TRANSCRICAO_LOCAL_ATIVA).')
    modelo = carregar_modelo()
    limite = int(getattr(settings, 'TRANSCRICAO_LOCAL_MAX_SEGUNDOS', 180) or 180)
    trava = _pasta_modelos() / '.transcrevendo.lock'
    with open(trava, 'w') as arquivo_trava:
        # Um por vez no servidor: os outros esperam aqui (estao em threads de fundo).
        fcntl.flock(arquivo_trava, fcntl.LOCK_EX)
        try:
            segmentos, info = modelo.transcribe(
                caminho,
                language='pt',
                vad_filter=True,
                beam_size=1,
                condition_on_previous_text=False,
            )
            duracao = float(getattr(info, 'duration', 0) or 0)
            if duracao > limite:
                raise TranscricaoLocalIndisponivel(
                    f'Audio de {int(duracao)} s passa do limite local de {limite} s.'
                )
            texto = ' '.join(segmento.text.strip() for segmento in segmentos).strip()
        finally:
            fcntl.flock(arquivo_trava, fcntl.LOCK_UN)
    return {'texto': texto, 'modelo': _modelo_nome, 'duracao': duracao}
