"""Envio de WhatsApp via Evolution API (Baileys / WhatsApp Web).

Alternativa a `twilio_service.py` para avaliar a troca da Twilio. Usa apenas a
stdlib (urllib) — sem novas dependencias. A instancia precisa estar conectada
(QR pareado) para o envio funcionar. Config em settings (EVOLUTION_*).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings


class EvolutionWhatsAppError(RuntimeError):
    pass


def _digits(phone: str) -> str:
    """Evolution espera so digitos com DDI (ex.: 5511999999999). Remove +, espacos, etc."""
    digits = re.sub(r'\D', '', phone or '')
    if not digits:
        raise EvolutionWhatsAppError('Telefone de destino invalido ou vazio.')
    return digits


def _request(method: str, path: str, payload: dict | None = None, timeout: int | None = None) -> tuple[int, Any]:
    base = (settings.EVOLUTION_BASE_URL or '').rstrip('/')
    if not base:
        raise EvolutionWhatsAppError('URL da API de WhatsApp nao configurada.')
    url = base + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('apikey', settings.EVOLUTION_API_KEY or '')
    req.add_header('Content-Type', 'application/json')
    if timeout is None:
        timeout = getattr(settings, 'EVOLUTION_REQUEST_TIMEOUT_SECONDS', 30)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', 'replace')
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, {'_raw': body}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'replace')
        try:
            detalhe = json.loads(body)
        except ValueError:
            detalhe = body
        raise EvolutionWhatsAppError(f'API de WhatsApp HTTP {exc.code}: {detalhe}') from exc
    except urllib.error.URLError as exc:
        raise EvolutionWhatsAppError(
            f'Falha ao conectar na API de WhatsApp ({base}): {exc.reason}. O servico esta em execucao?'
        ) from exc


def _post(path: str, payload: dict, timeout: int | None = None) -> tuple[int, Any]:
    return _request('POST', path, payload, timeout)


def send_whatsapp_text(*, to_phone: str, text: str) -> dict[str, Any]:
    """Envia texto simples. Retorna dict normalizado (sid/status/to/raw)."""
    if not (text or '').strip():
        raise EvolutionWhatsAppError('Mensagem vazia: informe um texto para enviar.')

    instance = settings.EVOLUTION_INSTANCE
    number = _digits(to_phone)
    status, data = _post(f'/message/sendText/{instance}', {'number': number, 'text': text})

    if not isinstance(data, dict):
        raise EvolutionWhatsAppError(f'Resposta inesperada da API de WhatsApp: {data!r}')

    key = data.get('key') or {}
    sid = key.get('id') or data.get('id') or ''
    if not sid:
        # Sem id de mensagem normalmente indica erro logico (instancia desconectada, etc.).
        raise EvolutionWhatsAppError(f'API de WhatsApp nao retornou id de mensagem: {data!r}')

    return {
        'sid': sid,
        'status': (data.get('status') or 'PENDING'),
        'to': number,
        'raw': data,
    }


# ---------------------------------------------------------------------------
# Gerenciamento da instancia (usado pela pagina de configuracao e pelo indicador
# de status no menu). Nao envia mensagens.
# ---------------------------------------------------------------------------
def get_connection_state(timeout: int = 5) -> str | None:
    """Estado da conexao: 'open' | 'connecting' | 'close' | None (None = API inacessivel)."""
    instance = settings.EVOLUTION_INSTANCE
    try:
        status, data = _request('GET', f'/instance/connectionState/{instance}', timeout=timeout)
    except EvolutionWhatsAppError:
        return None
    if status != 200 or not isinstance(data, dict):
        return None
    return (data.get('instance') or {}).get('state')


def create_instance() -> dict:
    """Cria a instancia. Idempotente: se ja existe, apenas segue (o connect gera o QR)."""
    instance = settings.EVOLUTION_INSTANCE
    try:
        _status, data = _post('/instance/create', {
            'instanceName': instance,
            'integration': 'WHATSAPP-BAILEYS',
            'qrcode': True,
        })
    except EvolutionWhatsAppError as exc:
        texto = str(exc).lower()
        if 'already' in texto or 'exists' in texto or 'in use' in texto:
            return {}
        raise
    return data if isinstance(data, dict) else {}


def connect_qr() -> dict:
    """Gera/retorna o QR para parear. Dict com 'base64' (imagem) e 'pairingCode'."""
    instance = settings.EVOLUTION_INSTANCE
    _status, data = _request('GET', f'/instance/connect/{instance}')
    return data if isinstance(data, dict) else {}


def logout_instance() -> dict:
    """Desconecta o WhatsApp pareado (encerra a sessao Baileys)."""
    instance = settings.EVOLUTION_INSTANCE
    _status, data = _request('DELETE', f'/instance/logout/{instance}')
    return data if isinstance(data, dict) else {}
