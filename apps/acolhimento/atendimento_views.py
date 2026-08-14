"""Telas (superusuario) para configurar o atendimento automatico (bot de menu).

- ConfiguracaoAtendimentoBotView: liga/desliga, textos, preview do menu e lista de opcoes.
- Opcao*View: CRUD + reordenacao das opcoes do menu (espelha o builder de questionario).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, UpdateView

from apps.acolhimento import atendimento_bot
from apps.acolhimento.forms import ConfiguracaoAtendimentoBotForm, OpcaoAtendimentoBotForm
from apps.acolhimento.models import (
    ConfiguracaoAtendimentoBot,
    ConfiguracaoProcessamentoFila,
    OpcaoAtendimentoBot,
)


class _SuperUserOnly(LoginRequiredMixin, UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        return self.request.user.is_superuser


class ConfiguracaoAtendimentoBotView(_SuperUserOnly, View):
    template_name = 'configuracao_atendimento.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, self._contexto())

    def post(self, request, *args, **kwargs):
        acao = request.POST.get('acao', '')
        cfg = ConfiguracaoAtendimentoBot.carregar()

        if acao == 'toggle':
            cfg.ativo = not cfg.ativo
            cfg.atualizado_por = request.user
            cfg.save(update_fields=['ativo', 'atualizado_por', 'atualizado_em'])
            if cfg.ativo:
                messages.success(request, 'Atendimento automatico LIGADO.')
            else:
                messages.info(request, 'Atendimento automatico DESLIGADO.')
            return redirect('configuracao-atendimento')

        if acao == 'textos':
            form = ConfiguracaoAtendimentoBotForm(request.POST, instance=cfg)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.atualizado_por = request.user
                obj.save()
                messages.success(request, 'Textos do atendimento atualizados.')
                return redirect('configuracao-atendimento')
            messages.error(request, 'Nao foi possivel salvar os textos. Verifique os campos.')
            return render(request, self.template_name, self._contexto(form=form))

        messages.error(request, 'Acao invalida.')
        return redirect('configuracao-atendimento')

    def _contexto(self, form=None):
        cfg = ConfiguracaoAtendimentoBot.carregar()
        opcoes = list(OpcaoAtendimentoBot.objects.all().order_by('ordem', 'id'))
        ativas = [op for op in opcoes if op.ativa]
        return {
            'cfg': cfg,
            'form': form or ConfiguracaoAtendimentoBotForm(instance=cfg),
            'opcoes': opcoes,
            'total_opcoes_ativas': len(ativas),
            'menu_preview': atendimento_bot.montar_menu(ativas),
            'processamento_auto_ligado': ConfiguracaoProcessamentoFila.auto_ligado(),
        }


class OpcaoAtendimentoBotCreateView(_SuperUserOnly, CreateView):
    template_name = 'opcao_atendimento_form.html'
    model = OpcaoAtendimentoBot
    form_class = OpcaoAtendimentoBotForm
    success_url = reverse_lazy('configuracao-atendimento')

    def form_valid(self, form):
        proxima = (OpcaoAtendimentoBot.objects.aggregate(m=Max('ordem'))['m'] or 0) + 1
        form.instance.ordem = proxima
        messages.success(self.request, 'Opcao criada.')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['titulo'] = 'Nova opcao'
        return ctx


class OpcaoAtendimentoBotUpdateView(_SuperUserOnly, UpdateView):
    template_name = 'opcao_atendimento_form.html'
    model = OpcaoAtendimentoBot
    form_class = OpcaoAtendimentoBotForm
    context_object_name = 'opcao'
    success_url = reverse_lazy('configuracao-atendimento')

    def form_valid(self, form):
        messages.success(self.request, 'Opcao atualizada.')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['titulo'] = 'Editar opcao'
        return ctx


class OpcaoAtendimentoBotDeleteView(_SuperUserOnly, View):
    def post(self, request, pk, *args, **kwargs):
        opcao = get_object_or_404(OpcaoAtendimentoBot, pk=pk)
        opcao.delete()
        messages.success(request, 'Opcao removida.')
        return redirect('configuracao-atendimento')


class OpcaoAtendimentoBotMoverView(_SuperUserOnly, View):
    def post(self, request, pk, *args, **kwargs):
        opcao = get_object_or_404(OpcaoAtendimentoBot, pk=pk)
        direcao = request.POST.get('direcao')
        opcoes = list(OpcaoAtendimentoBot.objects.all().order_by('ordem', 'id'))
        ids = [op.id for op in opcoes]
        indice = ids.index(opcao.id)
        alvo = indice - 1 if direcao == 'subir' else indice + 1
        if 0 <= alvo < len(opcoes):
            opcoes[indice], opcoes[alvo] = opcoes[alvo], opcoes[indice]
            for nova_ordem, item in enumerate(opcoes, start=1):
                if item.ordem != nova_ordem:
                    item.ordem = nova_ordem
                    item.save(update_fields=['ordem'])
        return redirect('configuracao-atendimento')
