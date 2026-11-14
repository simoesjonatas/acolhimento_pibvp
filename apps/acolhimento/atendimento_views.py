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

from apps.acolhimento import atendimento_bot, atendimento_ia, ia_service, transcricao_local
from apps.acolhimento.forms import (
    ConfiguracaoAtendimentoBotForm,
    ConfiguracaoIAForm,
    OpcaoAtendimentoBotForm,
    TestarIAForm,
)
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

        if acao == 'ia':
            form_ia = ConfiguracaoIAForm(request.POST, instance=cfg)
            if form_ia.is_valid():
                obj = form_ia.save(commit=False)
                obj.atualizado_por = request.user
                obj.save()
                messages.success(request, 'Configuracao da IA atualizada.')
                return redirect('configuracao-atendimento')
            messages.error(request, 'Nao foi possivel salvar a IA. Verifique os campos.')
            return render(request, self.template_name, self._contexto(form_ia=form_ia))

        if acao == 'testar_ia':
            form_teste = TestarIAForm(request.POST)
            resultado = None
            if form_teste.is_valid():
                if not ia_service.ollama_configurada():
                    resultado = {'erro': 'OLLAMA_API_KEY nao configurada no servidor.'}
                else:
                    resultado = atendimento_ia.testar(form_teste.cleaned_data['pergunta'])
            return render(
                request, self.template_name, self._contexto(form_teste=form_teste, resultado_teste=resultado),
            )

        messages.error(request, 'Acao invalida.')
        return redirect('configuracao-atendimento')

    def _contexto(self, form=None, form_ia=None, form_teste=None, resultado_teste=None):
        cfg = ConfiguracaoAtendimentoBot.carregar()
        opcoes = list(OpcaoAtendimentoBot.objects.all().order_by('ordem', 'id'))
        ativas = [op for op in opcoes if op.ativa]
        no_menu = atendimento_bot.opcoes_do_menu(ativas)
        # Numero que a pessoa ve no WhatsApp (so para quem esta ativa e no menu).
        numeros = {op.pk: numero for numero, op in enumerate(no_menu, start=1)}
        for op in opcoes:
            op.numero_menu = numeros.get(op.pk)
        return {
            'cfg': cfg,
            'form': form or ConfiguracaoAtendimentoBotForm(instance=cfg),
            'opcoes': opcoes,
            'total_opcoes_ativas': len(ativas),
            'total_opcoes_no_menu': len(no_menu),
            'menu_preview': atendimento_bot.montar_menu(ativas),
            'processamento_auto_ligado': ConfiguracaoProcessamentoFila.auto_ligado(),
            'form_ia': form_ia or ConfiguracaoIAForm(instance=cfg),
            'form_teste': form_teste or TestarIAForm(),
            'resultado_teste': resultado_teste,
            'ollama_configurada': ia_service.ollama_configurada(),
            'groq_configurada': ia_service.groq_configurada(),
            'transcricao_local_disponivel': transcricao_local.disponivel(),
            'modelo_padrao_ia': ia_service.modelo_padrao(),
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
        arquivo = opcao.midia_arquivo.name if opcao.midia_arquivo else ''
        opcao.delete()
        if arquivo:
            OpcaoAtendimentoBot._meta.get_field('midia_arquivo').storage.delete(arquivo)
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
