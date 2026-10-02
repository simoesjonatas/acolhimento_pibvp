"""Telas (superusuario) para configurar as opcoes do campo "Como conheceu".

Espelha o CRUD de opcoes do atendimento automatico: lista, cria, edita,
ativa/desativa, reordena e exclui.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.acolhimento.forms import OpcaoComoConheceuForm
from apps.acolhimento.models import OpcaoComoConheceu


class _SuperUserOnly(LoginRequiredMixin, UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        return self.request.user.is_superuser


class OpcaoComoConheceuListView(_SuperUserOnly, ListView):
    template_name = 'configuracao_como_conheceu.html'
    model = OpcaoComoConheceu
    context_object_name = 'opcoes'


class OpcaoComoConheceuCreateView(_SuperUserOnly, CreateView):
    template_name = 'opcao_como_conheceu_form.html'
    model = OpcaoComoConheceu
    form_class = OpcaoComoConheceuForm
    success_url = reverse_lazy('configuracao-como-conheceu')

    def form_valid(self, form):
        proxima = (OpcaoComoConheceu.objects.aggregate(m=Max('ordem'))['m'] or 0) + 1
        form.instance.ordem = proxima
        messages.success(self.request, 'Opcao criada.')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['titulo'] = 'Nova opcao'
        return ctx


class OpcaoComoConheceuUpdateView(_SuperUserOnly, UpdateView):
    template_name = 'opcao_como_conheceu_form.html'
    model = OpcaoComoConheceu
    form_class = OpcaoComoConheceuForm
    context_object_name = 'opcao'
    success_url = reverse_lazy('configuracao-como-conheceu')

    def form_valid(self, form):
        messages.success(self.request, 'Opcao atualizada.')
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['titulo'] = 'Editar opcao'
        return ctx


class OpcaoComoConheceuToggleView(_SuperUserOnly, View):
    def post(self, request, pk, *args, **kwargs):
        opcao = get_object_or_404(OpcaoComoConheceu, pk=pk)
        opcao.ativa = not opcao.ativa
        opcao.save(update_fields=['ativa', 'atualizado_em'])
        if opcao.ativa:
            messages.success(request, f'Opcao "{opcao.rotulo}" ativada.')
        else:
            messages.info(request, f'Opcao "{opcao.rotulo}" desativada.')
        return redirect('configuracao-como-conheceu')


class OpcaoComoConheceuDeleteView(_SuperUserOnly, View):
    def post(self, request, pk, *args, **kwargs):
        opcao = get_object_or_404(OpcaoComoConheceu, pk=pk)
        if opcao.em_uso():
            # Excluir deixaria cadastros antigos sem o rotulo; desativar preserva o historico.
            messages.error(
                request,
                f'A opcao "{opcao.rotulo}" ja foi usada em cadastros e nao pode ser excluida. Desative-a.',
            )
            return redirect('configuracao-como-conheceu')
        opcao.delete()
        messages.success(request, 'Opcao removida.')
        return redirect('configuracao-como-conheceu')


class OpcaoComoConheceuMoverView(_SuperUserOnly, View):
    def post(self, request, pk, *args, **kwargs):
        opcao = get_object_or_404(OpcaoComoConheceu, pk=pk)
        direcao = request.POST.get('direcao')
        opcoes = list(OpcaoComoConheceu.objects.all().order_by('ordem', 'id'))
        ids = [op.id for op in opcoes]
        indice = ids.index(opcao.id)
        alvo = indice - 1 if direcao == 'subir' else indice + 1
        if 0 <= alvo < len(opcoes):
            opcoes[indice], opcoes[alvo] = opcoes[alvo], opcoes[indice]
            for nova_ordem, item in enumerate(opcoes, start=1):
                if item.ordem != nova_ordem:
                    item.ordem = nova_ordem
                    item.save(update_fields=['ordem'])
        return redirect('configuracao-como-conheceu')
