"""Clientes HTTP da IA do robo: Ollama Cloud (texto) e Groq Whisper (audio).

So stdlib (urllib), no mesmo padrao de `meta_service`. Nenhuma chamada sai daqui
sem chave configurada; os erros viram `IAError` com o status HTTP, para quem chama
decidir entre tentar de novo, usar a reserva local ou cair no menu.

Referencias:
  - https://docs.ollama.com/api/chat (POST https://ollama.com/api/chat)
  - https://console.groq.com/docs/speech-to-text
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

from django.conf import settings

# A Groq fica atras do Cloudflare, que bloqueia o User-Agent padrao do urllib
# ("Python-urllib/3.x") com 403 "error code: 1010".
USER_AGENT = 'acolhimento-pibvp/1.0'


class IAError(RuntimeError):
    """Falha ao falar com a IA. `http_status` 0 = sem resposta (rede, timeout)."""

    def __init__(self, message: str, *, http_status: int = 0, detalhe: Any = None):
        super().__init__(message)
        self.http_status = http_status
        self.detalhe = detalhe

    @property
    def limite_ou_fora_do_ar(self) -> bool:
        """429 (limite), 5xx ou sem resposta: vale tentar outro motor."""
        return self.http_status == 0 or self.http_status == 429 or self.http_status >= 500


def ollama_configurada() -> bool:
    return bool((getattr(settings, 'OLLAMA_API_KEY', '') or '').strip())


def groq_configurada() -> bool:
    return bool((getattr(settings, 'GROQ_API_KEY', '') or '').strip())


def modelo_padrao() -> str:
    return (getattr(settings, 'OLLAMA_MODELO', '') or 'gemma4:31b').strip()


def _mensagem_de_erro(corpo: Any, padrao: str) -> str:
    if isinstance(corpo, dict):
        erro = corpo.get('error')
        if isinstance(erro, dict):
            return str(erro.get('message') or padrao)
        if erro:
            return str(erro)
    return padrao


def _abrir(req: urllib.request.Request, timeout: int) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            corpo = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        texto = exc.read().decode('utf-8', 'replace')
        try:
            detalhe = json.loads(texto)
        except ValueError:
            detalhe = {'_raw': texto[:500]}
        raise IAError(
            f'HTTP {exc.code}: {_mensagem_de_erro(detalhe, texto[:200] or exc.reason)}',
            http_status=exc.code,
            detalhe=detalhe,
        ) from exc
    except urllib.error.URLError as exc:
        raise IAError(f'Sem conexao com a IA: {exc.reason}.') from exc
    except TimeoutError as exc:
        raise IAError('A IA demorou demais para responder.') from exc
    try:
        return json.loads(corpo)
    except ValueError as exc:
        raise IAError(f'Resposta invalida da IA: {corpo[:200]!r}') from exc


def chat(messages: list[dict[str, str]], *, modelo: str = '', timeout: int | None = None) -> dict[str, Any]:
    """Uma rodada de chat na Ollama Cloud. Devolve {texto, modelo, tokens_entrada, tokens_saida}."""
    chave = (getattr(settings, 'OLLAMA_API_KEY', '') or '').strip()
    if not chave:
        raise IAError('OLLAMA_API_KEY nao configurada.')
    modelo = (modelo or '').strip() or modelo_padrao()
    payload = {
        'model': modelo,
        'messages': messages,
        'stream': False,
        'think': False,
        'options': {'temperature': 0.3, 'num_predict': 500},
    }
    base = (getattr(settings, 'OLLAMA_BASE_URL', '') or 'https://ollama.com').rstrip('/')
    req = urllib.request.Request(f'{base}/api/chat', data=json.dumps(payload).encode('utf-8'), method='POST')
    req.add_header('Authorization', f'Bearer {chave}')
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', USER_AGENT)
    if timeout is None:
        timeout = int(getattr(settings, 'IA_TIMEOUT_SECONDS', 40) or 40)
    data = _abrir(req, timeout)
    mensagem = data.get('message') if isinstance(data, dict) else None
    texto = str((mensagem or {}).get('content') or '').strip() if isinstance(mensagem, dict) else ''
    if not texto:
        raise IAError(f'A IA respondeu vazio: {str(data)[:200]}')
    return {
        'texto': texto,
        'modelo': str(data.get('model') or modelo),
        'tokens_entrada': int(data.get('prompt_eval_count') or 0),
        'tokens_saida': int(data.get('eval_count') or 0),
    }


def transcrever_groq(conteudo: bytes, *, nome: str, mime: str, timeout: int = 60) -> str:
    """Audio -> texto pela Groq (Whisper). Levanta IAError (429 = limite do plano gratis)."""
    chave = (getattr(settings, 'GROQ_API_KEY', '') or '').strip()
    if not chave:
        raise IAError('GROQ_API_KEY nao configurada.')
    if not conteudo:
        raise IAError('Audio vazio.', http_status=400)
    mime = (mime or 'audio/ogg').split(';')[0].strip() or 'audio/ogg'
    nome = re.sub(r'[\r\n"]', '', nome or 'audio.ogg') or 'audio.ogg'
    fronteira = f'----acolhimento{uuid.uuid4().hex}'
    campos = (
        ('model', (getattr(settings, 'GROQ_WHISPER_MODELO', '') or 'whisper-large-v3-turbo').strip()),
        ('language', 'pt'),
        ('response_format', 'json'),
        ('temperature', '0'),
    )
    partes = [
        f'--{fronteira}\r\nContent-Disposition: form-data; name="{campo}"\r\n\r\n{valor}\r\n'.encode('utf-8')
        for campo, valor in campos
    ]
    partes.append(
        (
            f'--{fronteira}\r\nContent-Disposition: form-data; name="file"; filename="{nome}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'
        ).encode('utf-8')
        + conteudo
        + b'\r\n'
    )
    partes.append(f'--{fronteira}--\r\n'.encode('utf-8'))

    base = (getattr(settings, 'GROQ_BASE_URL', '') or 'https://api.groq.com/openai/v1').rstrip('/')
    req = urllib.request.Request(f'{base}/audio/transcriptions', data=b''.join(partes), method='POST')
    req.add_header('Authorization', f'Bearer {chave}')
    req.add_header('Content-Type', f'multipart/form-data; boundary={fronteira}')
    req.add_header('User-Agent', USER_AGENT)
    data = _abrir(req, timeout)
    return str((data or {}).get('text') or '').strip() if isinstance(data, dict) else ''
