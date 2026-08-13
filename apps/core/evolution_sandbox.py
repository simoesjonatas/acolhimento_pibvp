"""Pagina de teste (sandbox) para avaliar o Evolution API como alternativa a Twilio.

ISOLADO de proposito: nao mexe em models, settings, fila ou no fluxo atual de Twilio.
Usa apenas a stdlib (urllib) para falar com o Evolution rodando em Docker (localhost:8085).
Config vem de variaveis de ambiente com defaults que casam com evolution/docker-compose.yml.

Endpoints do Evolution usados aqui (v2):
  POST   /instance/create                 cria a instancia (sessao WhatsApp)
  GET    /instance/connect/{inst}         gera/retorna o QR code para parear
  GET    /instance/connectionState/{inst} estado: open (conectado) / connecting / close
  POST   /message/sendText/{inst}         envia mensagem de texto
  DELETE /instance/logout/{inst}          desconecta o WhatsApp
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from django.shortcuts import render


def _cfg() -> dict[str, str]:
    return {
        'base_url': os.getenv('EVOLUTION_BASE_URL', 'http://localhost:8085').rstrip('/'),
        'api_key': os.getenv('EVOLUTION_API_KEY', 'pibvp-teste-2026'),
        'instance': os.getenv('EVOLUTION_INSTANCE', 'pibvp-teste'),
    }


def _try_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {'_raw': text}


def _api(method: str, path: str, cfg: dict, payload: dict | None = None, timeout: int = 30):
    """Chama o Evolution. Retorna (status_code, dados). status_code 0 = falha de conexao."""
    url = cfg['base_url'] + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('apikey', cfg['api_key'])
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', 'replace')
            return resp.status, _try_json(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', 'replace')
        return exc.code, _try_json(body)
    except urllib.error.URLError as exc:
        return 0, {
            'error': (
                f'Nao consegui falar com a API de WhatsApp em {cfg["base_url"]}: {exc.reason}. '
                'O servico de conexao esta em execucao?'
            )
        }
    except Exception as exc:  # noqa: BLE001 - sandbox: mostra qualquer erro na tela
        return 0, {'error': str(exc)}


def _normalize_number(raw: str) -> str:
    """So digitos. Ex.: +55 (11) 99999-9999 -> 5511999999999."""
    return re.sub(r'\D', '', raw or '')


def _as_data_uri(qr: str | None) -> str | None:
    if not qr:
        return None
    return qr if qr.startswith('data:') else f'data:image/png;base64,{qr}'


def evolution_sandbox(request):
    cfg = _cfg()
    ctx: dict = {
        'base_url': cfg['base_url'],
        'instance': cfg['instance'],
        'manager_url': f'{cfg["base_url"]}/manager',
        'last_action': None,
        'last_status': None,
        'last_response_json': None,
        'qr_data_uri': None,
        'pairing_code': None,
        'sent_number': '',
        'sent_text': 'Teste de mensagem via WhatsApp (sandbox PIBVP).',
    }

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'create':
            status, data = _api('POST', '/instance/create', cfg, payload={
                'instanceName': cfg['instance'],
                'integration': 'WHATSAPP-BAILEYS',
                'qrcode': True,
            })
            ctx['last_action'], ctx['last_status'], ctx['last_response_json'] = 'Criar instancia', status, data
            qr = (data or {}).get('qrcode') or {}
            ctx['qr_data_uri'] = _as_data_uri(qr.get('base64'))
            ctx['pairing_code'] = qr.get('pairingCode') or qr.get('code')
            # Se a instancia ja existia (ou o create nao devolveu QR), pega o QR pelo connect.
            if not ctx['qr_data_uri']:
                s2, d2 = _api('GET', f'/instance/connect/{cfg["instance"]}', cfg)
                ctx['qr_data_uri'] = _as_data_uri((d2 or {}).get('base64'))
                ctx['pairing_code'] = ctx['pairing_code'] or (d2 or {}).get('pairingCode')

        elif action == 'connect':
            status, data = _api('GET', f'/instance/connect/{cfg["instance"]}', cfg)
            ctx['last_action'], ctx['last_status'], ctx['last_response_json'] = 'Gerar QR / conectar', status, data
            ctx['qr_data_uri'] = _as_data_uri((data or {}).get('base64'))
            ctx['pairing_code'] = (data or {}).get('pairingCode')

        elif action == 'send':
            number = _normalize_number(request.POST.get('number', ''))
            text = request.POST.get('text', '')
            ctx['sent_number'] = request.POST.get('number', '')
            ctx['sent_text'] = text
            status, data = _api('POST', f'/message/sendText/{cfg["instance"]}', cfg, payload={
                'number': number,
                'text': text,
            })
            ctx['last_action'], ctx['last_status'], ctx['last_response_json'] = 'Enviar mensagem', status, data

        elif action == 'logout':
            status, data = _api('DELETE', f'/instance/logout/{cfg["instance"]}', cfg)
            ctx['last_action'], ctx['last_status'], ctx['last_response_json'] = 'Desconectar', status, data

    # Estado atual da conexao (best-effort; 404 = instancia ainda nao criada).
    st_status, st_data = _api('GET', f'/instance/connectionState/{cfg["instance"]}', cfg)
    state = None
    if st_status == 200:
        state = ((st_data or {}).get('instance') or {}).get('state')
    ctx['state'] = state
    ctx['state_ok'] = state == 'open'
    ctx['reachable'] = st_status != 0

    if ctx['last_response_json'] is not None:
        ctx['last_response_pretty'] = json.dumps(ctx['last_response_json'], indent=2, ensure_ascii=False)

    return render(request, 'evolution_sandbox.html', ctx)
