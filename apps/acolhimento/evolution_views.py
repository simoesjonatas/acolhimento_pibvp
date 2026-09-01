"""Pagina de configuracao (superusuario) para conectar o WhatsApp via Evolution.

Gera o QR, mostra o estado da conexao e permite desconectar. Reutiliza os
helpers de `evolution_service`. Nao envia mensagens.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.cache import cache
from django.shortcuts import render
from django.views import View

from apps.acolhimento import evolution_service


def _as_data_uri(qr: str | None) -> str | None:
    if not qr:
        return None
    return qr if qr.startswith('data:') else f'data:image/png;base64,{qr}'


def _pairing_code(data: dict) -> str | None:
    """Devolve so o codigo de pareamento de verdade (string curta).

    A Evolution tambem devolve `code`, que e o payload bruto do QR (uma string
    enorme). Mostrar isso na tela estoura o layout e nao serve para o usuario.
    """
    codigo = (data or {}).get('pairingCode')
    if not codigo:
        return None
    codigo = str(codigo).strip()
    return codigo if 0 < len(codigo) <= 16 else None


class ConfiguracaoWhatsappConexaoView(LoginRequiredMixin, UserPassesTestMixin, View):
    template_name = 'configuracao_whatsapp.html'
    raise_exception = True

    def test_func(self):
        return self.request.user.is_superuser

    def _contexto(self, qr_data_uri=None, pairing_code=None):
        estado = evolution_service.get_connection_state(timeout=3)
        # Mantem o indicador do menu em sincronia com o que a pagina mostra.
        cache.set('wa_conn_estado', estado, 30)
        provider = (getattr(settings, 'WHATSAPP_PROVIDER', 'twilio') or 'twilio')
        provider_normalizado = provider.strip().lower()
        return {
            'provider_atual': provider,
            'provider_rotulo': 'Conexao direta' if provider_normalizado == 'evolution' else 'Twilio',
            'usando_evolution': provider_normalizado == 'evolution',
            'base_url': settings.EVOLUTION_BASE_URL,
            'public_url': settings.EVOLUTION_PUBLIC_URL,
            'instance': settings.EVOLUTION_INSTANCE,
            'manager_url': f'{settings.EVOLUTION_PUBLIC_URL}/manager',
            'webhook_url': settings.EVOLUTION_WEBHOOK_URL,
            'estado': estado,
            'estado_ok': estado == 'open',
            'reachable': estado is not None,
            'qr_data_uri': qr_data_uri,
            'pairing_code': pairing_code,
        }

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, self._contexto())

    def post(self, request, *args, **kwargs):
        acao = request.POST.get('acao', '')
        qr_data_uri = None
        pairing_code = None
        try:
            if acao == 'criar':
                evolution_service.create_instance()
                data = evolution_service.connect_qr()
                qr_data_uri = _as_data_uri(data.get('base64'))
                pairing_code = _pairing_code(data)
                if qr_data_uri:
                    messages.info(request, 'QR gerado. Escaneie no WhatsApp em "Aparelhos conectados".')
                else:
                    messages.success(request, 'Conexao pronta.')
            elif acao == 'conectar':
                if getattr(settings, 'EVOLUTION_AUTO_CONFIGURE_WEBHOOK', True):
                    evolution_service.configure_webhook()
                data = evolution_service.connect_qr()
                qr_data_uri = _as_data_uri(data.get('base64'))
                pairing_code = _pairing_code(data)
                if not qr_data_uri:
                    messages.info(request, 'Nenhum QR no momento (ja conectado ou aguardando).')
            elif acao == 'desconectar':
                evolution_service.logout_instance()
                messages.success(request, 'WhatsApp desconectado.')
            else:
                messages.error(request, 'Acao invalida.')
        except evolution_service.EvolutionWhatsAppError as exc:
            messages.error(request, f'Erro na conexao do WhatsApp: {exc}')

        cache.delete('wa_conn_estado')
        return render(request, self.template_name, self._contexto(qr_data_uri, pairing_code))
