"""Envio de WhatsApp pela Cloud API (API oficial da Meta / Graph API).

Terceiro provider ao lado de `twilio_service.py` e `evolution_service.py`. Fala
direto com o Graph API usando apenas a stdlib (urllib), sem novas dependencias.

Dois tipos de envio:
  - texto livre: so e aceito pela Meta dentro da janela de 24h apos a ultima
    mensagem da pessoa (regra aplicada antes, em `whatsapp_rules`);
  - template aprovado: identificado por NOME + IDIOMA; abre/reabre a conversa.

Referencia: https://developers.facebook.com/docs/whatsapp/cloud-api/messages
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings


class MetaWhatsAppError(RuntimeError):
    """Erro de envio/consulta na Cloud API. `code` e o codigo de erro do Graph (ou 0)."""

    def __init__(self, message: str, *, code: int = 0, http_status: int = 0, detalhe: Any = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.detalhe = detalhe


def _digits(phone: str) -> str:
    """A Cloud API espera o numero so com digitos e DDI (ex.: 5511999999999)."""
    digits = re.sub(r'\D', '', phone or '')
    if not digits:
        raise MetaWhatsAppError('Telefone de destino invalido ou vazio.')
    return digits


def _base_url() -> str:
    base = (getattr(settings, 'META_WA_GRAPH_BASE_URL', '') or 'https://graph.facebook.com').rstrip('/')
    versao = (getattr(settings, 'META_WA_API_VERSION', '') or 'v22.0').strip().strip('/')
    return f'{base}/{versao}'


def _token() -> str:
    token = (getattr(settings, 'META_WA_ACCESS_TOKEN', '') or '').strip()
    if not token:
        raise MetaWhatsAppError('META_WA_ACCESS_TOKEN nao configurado.')
    return token


def _phone_number_id() -> str:
    pnid = (getattr(settings, 'META_WA_PHONE_NUMBER_ID', '') or '').strip()
    if not pnid:
        raise MetaWhatsAppError('META_WA_PHONE_NUMBER_ID nao configurado.')
    return pnid


def _traduzir_erro(code: int, mensagem_graph: str) -> str:
    """Mensagem amigavel para os erros mais comuns da Cloud API (vao para a tela)."""
    # https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes
    conhecidos = {
        0: 'Token de acesso invalido ou expirado. Gere um token permanente de System User.',
        190: 'Token de acesso invalido ou expirado. Gere um token permanente de System User.',
        10: 'O token nao tem permissao para enviar mensagens (whatsapp_business_messaging).',
        200: 'O token nao tem permissao para esta acao.',
        4: 'Limite de chamadas a API atingido. Aguarde e tente de novo.',
        80007: 'Limite de envio do numero atingido. Aguarde e tente de novo.',
        130429: 'Limite de taxa do WhatsApp atingido. Aguarde e tente de novo.',
        131026: (
            'Mensagem nao entregue: o numero nao tem WhatsApp, bloqueou o contato ou '
            'nao aceitou os termos atuais do WhatsApp.'
        ),
        131047: (
            'Fora da janela de 24h: a pessoa nao escreveu nas ultimas 24h. '
            'Envie um template aprovado para reabrir a conversa.'
        ),
        131049: 'A Meta limitou esta mensagem de marketing para manter o engajamento saudavel.',
        131051: 'Tipo de mensagem nao suportado.',
        131000: 'Erro interno do WhatsApp. Tente de novo mais tarde.',
        132000: 'Numero de parametros diferente do que o template espera.',
        132001: 'Template nao existe (ou nao esta aprovado) para este nome/idioma.',
        132005: 'Texto do template nao bate com o template aprovado.',
        132012: 'Formato dos parametros do template invalido.',
        132015: 'Template pausado pela Meta por baixa qualidade.',
        132016: 'Template desativado pela Meta por baixa qualidade.',
        133010: 'O numero de telefone nao esta registrado na Cloud API.',
        131030: 'Numero de destino nao esta na lista de destinatarios permitidos (modo de teste).',
    }
    texto = conhecidos.get(code)
    if texto:
        return f'{texto} (Meta #{code}: {mensagem_graph})' if mensagem_graph else f'{texto} (Meta #{code})'
    return f'A API da Meta recusou a requisicao (#{code}): {mensagem_graph}' if code else (
        f'A API da Meta recusou a requisicao: {mensagem_graph}'
    )


def _request(
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    params: dict | None = None,
    timeout: int | None = None,
) -> tuple[int, Any]:
    url = f'{_base_url()}/{path.lstrip("/")}'
    if params:
        url = f'{url}?{urllib.parse.urlencode(params)}'
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', f'Bearer {_token()}')
    req.add_header('Content-Type', 'application/json')
    if timeout is None:
        timeout = getattr(settings, 'META_WA_REQUEST_TIMEOUT_SECONDS', 20)
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
            detalhe = {'_raw': body}
        erro = detalhe.get('error') if isinstance(detalhe, dict) else None
        erro = erro if isinstance(erro, dict) else {}
        code = int(erro.get('code') or 0)
        mensagem = str(erro.get('message') or '').strip()
        dados_erro = erro.get('error_data') or {}
        detalhes_extra = str(dados_erro.get('details') or '').strip() if isinstance(dados_erro, dict) else ''
        if detalhes_extra and detalhes_extra not in mensagem:
            mensagem = f'{mensagem} {detalhes_extra}'.strip()
        raise MetaWhatsAppError(
            _traduzir_erro(code, mensagem or f'HTTP {exc.code}'),
            code=code,
            http_status=exc.code,
            detalhe=detalhe,
        ) from exc
    except urllib.error.URLError as exc:
        raise MetaWhatsAppError(f'Falha ao conectar na API da Meta: {exc.reason}.') from exc


def _post_messages(payload: dict[str, Any]) -> dict[str, Any]:
    payload = {'messaging_product': 'whatsapp', 'recipient_type': 'individual', **payload}
    _status, data = _request('POST', f'{_phone_number_id()}/messages', payload)
    if not isinstance(data, dict):
        raise MetaWhatsAppError(f'Resposta inesperada da API da Meta: {data!r}')

    mensagens = data.get('messages') or []
    primeira = mensagens[0] if mensagens and isinstance(mensagens[0], dict) else {}
    sid = str(primeira.get('id') or '')
    if not sid:
        raise MetaWhatsAppError(f'API da Meta nao retornou id de mensagem: {data!r}')

    contatos = data.get('contacts') or []
    wa_id = str((contatos[0] or {}).get('wa_id') or '') if contatos and isinstance(contatos[0], dict) else ''
    return {
        'sid': sid,
        # 'accepted' (normal) ou 'held_for_quality_assessment' (marketing em avaliacao).
        'status': str(primeira.get('message_status') or 'accepted'),
        'to': payload.get('to', ''),
        'wa_id': wa_id,
        'raw': data,
    }


def send_text(*, to_phone: str, text: str) -> dict[str, Any]:
    """Texto livre (somente dentro da janela de 24h). Retorna dict normalizado."""
    if not (text or '').strip():
        raise MetaWhatsAppError('Mensagem vazia: informe um texto para enviar.')
    return _post_messages({
        'to': _digits(to_phone),
        'type': 'text',
        'text': {'preview_url': False, 'body': text},
    })


def montar_componentes(parametros: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Converte as variaveis ({"1": "Maria"}) nos `components` do template.

    Chaves numericas => parametros posicionais ({{1}}, {{2}}...), na ordem numerica.
    Chaves com nome => parametros nomeados ({{nome}}), com `parameter_name`.
    Sem variaveis => sem components (template sem placeholders).
    """
    parametros = {str(k): v for k, v in (parametros or {}).items() if str(k).strip()}
    if not parametros:
        return []

    if all(chave.isdigit() for chave in parametros):
        ordenados = sorted(parametros.items(), key=lambda item: int(item[0]))
        lista = [{'type': 'text', 'text': str(valor)} for _chave, valor in ordenados]
    else:
        lista = [
            {'type': 'text', 'parameter_name': chave, 'text': str(valor)}
            for chave, valor in parametros.items()
        ]
    return [{'type': 'body', 'parameters': lista}]


def send_template(
    *,
    to_phone: str,
    nome: str,
    idioma: str = 'pt_BR',
    parametros: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Template aprovado pela Meta (abre/reabre a conversa fora da janela de 24h)."""
    nome = (nome or '').strip()
    if not nome:
        raise MetaWhatsAppError('Nome do template nao informado.')
    template: dict[str, Any] = {
        'name': nome,
        'language': {'code': (idioma or 'pt_BR').strip() or 'pt_BR'},
    }
    componentes = montar_componentes(parametros)
    if componentes:
        template['components'] = componentes
    return _post_messages({
        'to': _digits(to_phone),
        'type': 'template',
        'template': template,
    })


def marcar_como_lida(message_id: str) -> bool:
    """Marca uma mensagem recebida como lida (tique azul para a pessoa). Best-effort."""
    message_id = (message_id or '').strip()
    if not message_id:
        return False
    try:
        _request('POST', f'{_phone_number_id()}/messages', {
            'messaging_product': 'whatsapp',
            'status': 'read',
            'message_id': message_id,
        })
    except MetaWhatsAppError:
        return False
    return True


# ---------------------------------------------------------------------------
# Consultas usadas pela pagina de configuracao e pelo indicador do menu.
# ---------------------------------------------------------------------------
def get_phone_number_info(timeout: int = 5) -> dict[str, Any] | None:
    """Dados do numero (nome verificado, qualidade, status). None = nao acessivel."""
    campos = 'display_phone_number,verified_name,quality_rating,code_verification_status,name_status,platform_type'
    try:
        status, data = _request('GET', _phone_number_id(), params={'fields': campos}, timeout=timeout)
    except MetaWhatsAppError:
        return None
    if status != 200 or not isinstance(data, dict):
        return None
    return data


def list_templates(timeout: int = 8) -> list[dict[str, Any]]:
    """Templates da WABA (nome, idioma, status, categoria). Lista vazia se nao der para consultar."""
    waba = (getattr(settings, 'META_WA_BUSINESS_ACCOUNT_ID', '') or '').strip()
    if not waba:
        return []
    try:
        status, data = _request(
            'GET',
            f'{waba}/message_templates',
            params={'fields': 'name,language,status,category,quality_score', 'limit': 100},
            timeout=timeout,
        )
    except MetaWhatsAppError:
        return []
    if status != 200 or not isinstance(data, dict):
        return []
    itens = data.get('data') or []
    return [item for item in itens if isinstance(item, dict)]


def get_pricing_analytics(inicio_ts: int, fim_ts: int, timeout: int = 10) -> list[dict[str, Any]] | None:
    """Custo e volume informados pela Meta no periodo (UNIX), por categoria e tipo.

    Usa o campo `pricing_analytics` da WABA. Devolve a lista de `data_points`
    ({start, end, pricing_category, pricing_type, volume, cost}) ou None quando nao
    da para consultar (sem WABA, token sem `whatsapp_business_management`, erro).
    O custo vem na moeda da conta de pagamento da Meta e e aproximado.
    """
    waba = (getattr(settings, 'META_WA_BUSINESS_ACCOUNT_ID', '') or '').strip()
    if not waba or not configurado():
        return None
    campo = (
        f'pricing_analytics.start({int(inicio_ts)}).end({int(fim_ts)})'
        '.granularity(DAILY).dimensions(PRICING_CATEGORY,PRICING_TYPE)'
    )
    try:
        status, data = _request('GET', waba, params={'fields': campo}, timeout=timeout)
    except MetaWhatsAppError:
        return None
    if status != 200 or not isinstance(data, dict):
        return None
    analytics = data.get('pricing_analytics') or {}
    pontos: list[dict[str, Any]] = []
    for bloco in analytics.get('data') or []:
        if not isinstance(bloco, dict):
            continue
        pontos.extend(item for item in bloco.get('data_points') or [] if isinstance(item, dict))
    return pontos


def configurado() -> bool:
    """True quando ha o minimo para enviar (token + phone number id)."""
    return bool(
        (getattr(settings, 'META_WA_ACCESS_TOKEN', '') or '').strip()
        and (getattr(settings, 'META_WA_PHONE_NUMBER_ID', '') or '').strip()
    )
