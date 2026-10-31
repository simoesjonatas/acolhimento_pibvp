"""Pagina de configuracao (superusuario) do WhatsApp na API oficial da Meta.

Nao ha QR para parear: o numero e registrado no painel da Meta. Esta tela mostra
se o app consegue falar com o Graph API (numero, nome verificado, qualidade),
se o webhook e os templates estao configurados, e permite um envio de teste.

`ConfiguracaoWhatsappView` e o despachante da rota `configuracao-whatsapp`: no
modo Meta renderiza esta tela; nos demais, a tela de conexao do Evolution.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.cache import cache
from django.shortcuts import render
from django.views import View

from apps.acolhimento import meta_service, template_config
from apps.acolhimento.evolution_views import ConfiguracaoWhatsappConexaoView
from apps.acolhimento.fila_processor import normalize_phone_br
from apps.acolhimento.models import TemplateWhatsapp


def _mascarar(valor: str, visiveis: int = 4) -> str:
    valor = (valor or '').strip()
    if not valor:
        return ''
    if len(valor) <= visiveis:
        return '*' * len(valor)
    return '*' * (len(valor) - visiveis) + valor[-visiveis:]


class ConfiguracaoWhatsappMetaView(LoginRequiredMixin, UserPassesTestMixin, View):
    template_name = 'configuracao_whatsapp_meta.html'
    raise_exception = True

    def test_func(self):
        return self.request.user.is_superuser

    def _contexto(self):
        configurado = meta_service.configurado()
        info = meta_service.get_phone_number_info(timeout=4) if configurado else None
        templates = meta_service.list_templates(timeout=6) if configurado else []
        # Mantem o indicador do menu em sincronia com o que a pagina mostra.
        cache.set('wa_conn_estado', 'open' if info is not None else None, 30)

        nomes_configurados = {
            TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO: template_config.opt_in_meta_nome(),
            TemplateWhatsapp.Tipo.CONTINUAR: template_config.continuar_meta_nome(),
        }
        status_por_nome = {
            (item.get('name') or '', item.get('language') or ''): item.get('status') or ''
            for item in templates
        }

        def _status_template(tipo, idioma):
            nome = nomes_configurados[tipo]
            if not nome:
                return 'nao configurado'
            if not templates:
                return 'configurado'
            return (status_por_nome.get((nome, idioma)) or 'nao encontrado na WABA').lower()

        return {
            'provider_atual': template_config.provider(),
            'configurado': configurado,
            'reachable': info is not None,
            'info': info or {},
            'phone_number_id': settings.META_WA_PHONE_NUMBER_ID,
            'waba_id': settings.META_WA_BUSINESS_ACCOUNT_ID,
            'api_version': settings.META_WA_API_VERSION,
            'token_mascarado': _mascarar(settings.META_WA_ACCESS_TOKEN),
            'app_secret_ok': bool(settings.META_WA_APP_SECRET),
            'verify_token_ok': bool(settings.META_WA_VERIFY_TOKEN),
            'webhook_url': settings.META_WA_WEBHOOK_URL,
            'templates': templates,
            'opt_in_nome': nomes_configurados[TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO],
            'opt_in_idioma': template_config.opt_in_meta_idioma(),
            'opt_in_status': _status_template(
                TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, template_config.opt_in_meta_idioma()
            ),
            'continuar_nome': nomes_configurados[TemplateWhatsapp.Tipo.CONTINUAR],
            'continuar_idioma': template_config.continuar_meta_idioma(),
            'continuar_status': _status_template(
                TemplateWhatsapp.Tipo.CONTINUAR, template_config.continuar_meta_idioma()
            ),
        }

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, self._contexto())

    def post(self, request, *args, **kwargs):
        acao = request.POST.get('acao', '')
        if acao == 'teste':
            self._enviar_teste(request)
        else:
            messages.error(request, 'Acao invalida.')
        cache.delete('wa_conn_estado')
        return render(request, self.template_name, self._contexto())

    def _enviar_teste(self, request):
        """Envia o template de boas-vindas (ou um texto) para um numero digitado pelo superusuario."""
        telefone = (request.POST.get('telefone') or '').strip()
        modo = (request.POST.get('modo') or 'template').strip()
        try:
            destino = normalize_phone_br(telefone)
        except ValueError as exc:
            messages.error(request, f'Telefone invalido: {exc}')
            return

        try:
            if modo == 'texto':
                texto = (request.POST.get('texto') or '').strip() or 'Teste de integracao do acolhimento PIBVP.'
                resultado = meta_service.send_text(to_phone=destino, text=texto)
            else:
                tipo = TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO
                nome_template = template_config.opt_in_meta_nome()
                if not nome_template:
                    messages.error(request, template_config.mensagem_nao_configurado(tipo))
                    return
                pessoa_fake = type('PessoaTeste', (), {'nome': 'Teste'})()
                resultado = meta_service.send_template(
                    to_phone=destino,
                    nome=nome_template,
                    idioma=template_config.opt_in_meta_idioma(),
                    parametros=template_config.parametros_meta_para(tipo, pessoa_fake),
                )
        except meta_service.MetaWhatsAppError as exc:
            messages.error(request, f'A Meta recusou o envio de teste: {exc}')
            return

        messages.success(
            request,
            f'Mensagem de teste aceita pela Meta (status {resultado.get("status")}, id {resultado.get("sid")}). '
            'A entrega chega pelo webhook.',
        )


class ConfiguracaoWhatsappView(View):
    """Despacha a rota `configuracao-whatsapp` para a tela do provider ativo."""

    def dispatch(self, request, *args, **kwargs):
        if template_config.usando_meta():
            view = ConfiguracaoWhatsappMetaView.as_view()
        else:
            view = ConfiguracaoWhatsappConexaoView.as_view()
        return view(request, *args, **kwargs)
