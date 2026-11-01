"""Relatorio de custo do WhatsApp (API oficial da Meta). Somente superusuario."""
from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.acolhimento import custo_whatsapp, template_config
from apps.acolhimento.forms import PeriodoCustoWhatsappForm, TabelaPrecoWhatsappForm
from apps.acolhimento.models import TabelaPrecoWhatsapp
from apps.acolhimento.views import SuperAdminPermissaoMixin


class RelatorioCustoWhatsappView(LoginRequiredMixin, SuperAdminPermissaoMixin, View):
    """Anonimo vai para o login; logado sem acesso administrativo recebe 403."""

    template_name = 'relatorio_custo_whatsapp.html'

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            raise PermissionDenied('Somente administradores podem ver o custo do WhatsApp.')
        return super().handle_no_permission()

    def _periodo(self, request) -> tuple[date, date, PeriodoCustoWhatsappForm]:
        hoje = timezone.localdate()
        atalho = request.GET.get('atalho', '')
        if atalho == 'mes_anterior':
            inicio, fim = custo_whatsapp.periodo_mes_anterior(hoje)
        else:
            inicio, fim = custo_whatsapp.periodo_padrao(hoje)

        if 'inicio' in request.GET or 'fim' in request.GET:
            form = PeriodoCustoWhatsappForm(request.GET)
            if form.is_valid():
                return form.cleaned_data['inicio'], form.cleaned_data['fim'], form
            return inicio, fim, form
        return inicio, fim, PeriodoCustoWhatsappForm(initial={'inicio': inicio, 'fim': fim})

    def get(self, request, *args, **kwargs):
        inicio, fim, periodo_form = self._periodo(request)
        tabela = TabelaPrecoWhatsapp.carregar()

        if request.GET.get('exportar') == 'csv':
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            nome = f'custo_whatsapp_{inicio:%Y%m%d}_{fim:%Y%m%d}.csv'
            response['Content-Disposition'] = f'attachment; filename="{nome}"'
            response.write('\ufeff')  # BOM: o Excel abre os acentos certos
            custo_whatsapp.escrever_csv(response, inicio, fim, tabela=tabela)
            return response

        return render(request, self.template_name, self._contexto(
            inicio, fim, periodo_form, TabelaPrecoWhatsappForm(instance=tabela), tabela,
        ))

    def post(self, request, *args, **kwargs):
        """Atualiza a tabela de precos usada no custo estimado."""
        tabela = TabelaPrecoWhatsapp.carregar()
        form = TabelaPrecoWhatsappForm(request.POST, instance=tabela)
        if form.is_valid():
            tabela = form.save(commit=False)
            tabela.atualizado_por = request.user
            tabela.save()
            messages.success(request, 'Tabela de precos atualizada.')
            destino = reverse('relatorio-custo-whatsapp')
            consulta = request.POST.get('consulta', '')
            return redirect(f'{destino}?{consulta}' if consulta else destino)

        messages.error(request, 'Nao foi possivel salvar os precos. Verifique os campos.')
        inicio, fim, periodo_form = self._periodo(request)
        return render(request, self.template_name, self._contexto(inicio, fim, periodo_form, form, tabela))

    def _contexto(self, inicio, fim, periodo_form, preco_form, tabela):
        oficial = custo_whatsapp.custo_oficial(inicio, fim) if template_config.usando_meta() else None
        dados = custo_whatsapp.resumo(inicio, fim, tabela=tabela, oficial=oficial)
        consulta = self.request.GET.urlencode()
        return {
            **dados,
            'periodo_form': periodo_form,
            'preco_form': preco_form,
            'usando_meta': template_config.usando_meta(),
            'moeda': tabela.moeda,
            'tabela_preenchida': tabela.preenchida(),
            'consulta': consulta,
            'url_csv': f"{reverse('relatorio-custo-whatsapp')}?{consulta}{'&' if consulta else ''}exportar=csv",
            'maior_dia': max((linha.total for linha in dados['por_dia']), default=0),
        }
