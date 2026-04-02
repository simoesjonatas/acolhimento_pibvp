from __future__ import annotations

from importlib import import_module
from typing import Any

from django.conf import settings


class TwilioWhatsAppError(RuntimeError):
    pass


def _load_twilio_sdk() -> tuple[type, type[Exception]]:
    try:
        rest_module = import_module('twilio.rest')
        exc_module = import_module('twilio.base.exceptions')
        client_class = getattr(rest_module, 'Client')
        twilio_exc_class = getattr(exc_module, 'TwilioRestException')
        return client_class, twilio_exc_class
    except Exception as exc:  # pragma: no cover
        raise TwilioWhatsAppError('Pacote twilio nao disponivel no ambiente Python.') from exc


def _as_whatsapp_address(phone: str) -> str:
    phone = (phone or '').strip()
    if not phone:
        raise TwilioWhatsAppError('Telefone de destino nao informado.')
    if phone.startswith('whatsapp:'):
        return phone
    return f'whatsapp:{phone}'


def _build_client() -> tuple[Any, type[Exception]]:
    if not settings.TWILIO_ENABLED:
        raise TwilioWhatsAppError('Integracao Twilio desativada. Defina TWILIO_ENABLED=true.')

    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise TwilioWhatsAppError('Credenciais Twilio ausentes. Defina TWILIO_ACCOUNT_SID e TWILIO_AUTH_TOKEN.')

    client_class, twilio_exc_class = _load_twilio_sdk()
    return client_class(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN), twilio_exc_class


def _get_status_callback_url() -> str:
    url = (settings.TWILIO_STATUS_CALLBACK_URL or '').strip()
    if not url:
        return ''
    if not url.startswith(('http://', 'https://')):
        return ''
    if 'seu-dominio' in url:
        return ''
    return url


def send_whatsapp_message(
    *,
    to_phone: str,
    body: str | None = None,
    content_sid: str | None = None,
    content_variables: str | None = None,
) -> dict[str, Any]:
    if not body and not content_sid:
        raise TwilioWhatsAppError('Informe body ou content_sid para enviar a mensagem.')

    client, twilio_exc_class = _build_client()
    payload: dict[str, Any] = {
        'from_': settings.TWILIO_WHATSAPP_FROM,
        'to': _as_whatsapp_address(to_phone),
    }

    status_callback_url = _get_status_callback_url()
    if status_callback_url:
        payload['status_callback'] = status_callback_url

    if content_sid:
        payload['content_sid'] = content_sid
        if content_variables:
            payload['content_variables'] = content_variables
    elif body:
        payload['body'] = body

    try:
        message = client.messages.create(**payload)
    except twilio_exc_class as exc:
        raise TwilioWhatsAppError(str(exc)) from exc

    return {
        'sid': message.sid,
        'status': message.status,
        'to': message.to,
        'from': message.from_,
        'raw': {
            'sid': message.sid,
            'status': message.status,
            'error_code': message.error_code,
            'error_message': message.error_message,
            'date_created': message.date_created.isoformat() if message.date_created else None,
        },
    }
