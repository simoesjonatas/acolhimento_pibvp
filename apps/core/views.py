import json

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db.models import Q
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from apps.acolhimento import template_config
from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato, TemplateWhatsapp
from apps.acolhimento.whatsapp_rules import contar_pessoas_janela_aberta
from apps.core.forms import PerfilForm, UsuarioCreateForm, UsuarioUpdateForm


User = get_user_model()


class UsuarioGestaoPermissaoMixin(UserPassesTestMixin):
	raise_exception = True

	def test_func(self):
		return self.request.user.is_superuser


class DashboardView(LoginRequiredMixin, TemplateView):
	template_name = 'dashboard.html'

	def get_pessoas_boas_vindas_queryset(self):
		return PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO
		)

	def post(self, request, *args, **kwargs):
		if not (request.user.is_staff or request.user.is_superuser):
			messages.error(request, 'Voce nao tem permissao para executar este disparo.')
			return redirect('dashboard')

		action = request.POST.get('action', '').strip()
		if action not in {'disparar_boas_vindas', 'disparar_template_opt_in'}:
			messages.error(request, 'Acao invalida para o dashboard.')
			return redirect('dashboard')

		if action == 'disparar_boas_vindas':
			messages.error(
				request,
				'Envio direto de boas-vindas por WhatsApp foi bloqueado. Use o template de primeiro contato.',
			)
			return redirect('dashboard')

		pessoas_primeiro_contato = list(self.get_pessoas_boas_vindas_queryset())

		if not pessoas_primeiro_contato:
			messages.info(request, 'Nao ha pessoas em Primeiro contato para disparo.')
			return redirect('dashboard')

		tipo_template = TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO
		usa_evolution = template_config.usando_evolution()
		template_sid = template_config.opt_in_sid()
		if action == 'disparar_template_opt_in' and not template_config.opt_in_configurado():
			mensagem_config = (
				'Mensagem de boas-vindas do WhatsApp nao configurada. '
				'Defina o texto na tela de configuracao de templates.'
				if usa_evolution
				else 'Template SID nao configurado. Defina o SID na tela de configuracao de templates.'
			)
			messages.error(request, mensagem_config)
			return redirect('dashboard')

		try:
			template_vars_base = json.loads(template_config.opt_in_variables() or '{}')
			if not isinstance(template_vars_base, dict):
				template_vars_base = {}
		except Exception:
			template_vars_base = {}
		texto_evolution_base = template_config.opt_in_texto_evolution() if usa_evolution else ''
		mensagens = []
		for pessoa in pessoas_primeiro_contato:
			nome = (pessoa.nome or '').strip() or 'amigo(a)'
			content_variables = json.dumps(
				{
					**template_vars_base,
					'1': nome,
				},
				ensure_ascii=False,
			)
			metadata_envio = {'tipo_template': tipo_template}
			if template_sid:
				metadata_envio['twilio_template'] = {
					'content_sid': template_sid,
					'content_variables': content_variables,
				}
			if usa_evolution:
				texto_evolution = texto_evolution_base.replace('{nome}', nome)
				metadata_envio['evolution_texto'] = texto_evolution
				conteudo = texto_evolution
			else:
				conteudo = 'Template opt-in enfileirado'

			mensagens.append(
				MensagemContato(
					pessoa=pessoa,
					criado_por=request.user,
					canal=MensagemContato.CanalChoices.WHATSAPP,
					direcao=MensagemContato.DirecaoChoices.SAIDA,
					status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
					prioridade=5,
					agendada_para=None,
					conteudo=conteudo,
					metadata_envio=metadata_envio,
				)
			)

		MensagemContato.objects.bulk_create(mensagens)
		PrimeiroContato.objects.filter(
			id__in=[pessoa.id for pessoa in pessoas_primeiro_contato]
		).update(status=PrimeiroContato.StatusAcolhimento.ROBO)

		# Processamento automatico: bulk_create nao emite signal, entao dispara aqui.
		from apps.acolhimento import fila_auto
		fila_auto.agendar_disparo_auto()

		messages.success(
			request,
			f'Disparo de boas-vindas criado para {len(mensagens)} pessoa(s) e status atualizado para Robo.',
		)
		return redirect('dashboard')

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)

		total_pessoas = PrimeiroContato.objects.count()
		total_primeiro_contato = self.get_pessoas_boas_vindas_queryset().count()
		total_robo = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.ROBO
		).count()
		total_acompanhamento = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
		).count()
		total_participante = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.PARTICIPANTE
		).count()
		total_membros = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.MEMBRO
		).count()
		total_janela_aberta = contar_pessoas_janela_aberta()

		ultimos_passos = InteracaoAcolhimento.objects.select_related('pessoa')[:8]

		context.update(
			{
				'total_pessoas': total_pessoas,
				'total_primeiro_contato': total_primeiro_contato,
				'total_boas_vindas': total_primeiro_contato,
				'total_robo': total_robo,
				'total_acompanhamento': total_acompanhamento,
				'total_participante': total_participante,
				'total_membros': total_membros,
				'total_janela_aberta': total_janela_aberta,
				'ultimos_passos': ultimos_passos,
			}
		)

		return context


class PerfilView(LoginRequiredMixin, TemplateView):
	template_name = 'perfil.html'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context['usuario_obj'] = self.request.user
		context['perfil_form'] = kwargs.get('perfil_form') or PerfilForm(instance=self.request.user)
		context['senha_form'] = kwargs.get('senha_form') or PasswordChangeForm(user=self.request.user)
		return context

	def post(self, request, *args, **kwargs):
		action = request.POST.get('action', '').strip()
		if action == 'salvar_perfil':
			perfil_form = PerfilForm(request.POST, instance=request.user)
			senha_form = PasswordChangeForm(user=request.user)
			if perfil_form.is_valid():
				perfil_form.save()
				messages.success(request, 'Dados do perfil atualizados com sucesso.')
				return redirect('perfil')
			messages.error(request, 'Nao foi possivel atualizar o perfil. Verifique os campos.')
			return self.render_to_response(self.get_context_data(perfil_form=perfil_form, senha_form=senha_form))

		if action == 'trocar_senha':
			perfil_form = PerfilForm(instance=request.user)
			senha_form = PasswordChangeForm(user=request.user, data=request.POST)
			if senha_form.is_valid():
				usuario = senha_form.save()
				update_session_auth_hash(request, usuario)
				messages.success(request, 'Senha atualizada com sucesso.')
				return redirect('perfil')
			messages.error(request, 'Nao foi possivel atualizar a senha. Verifique os campos.')
			return self.render_to_response(self.get_context_data(perfil_form=perfil_form, senha_form=senha_form))

		messages.error(request, 'Acao invalida.')
		return redirect('perfil')


class UsuarioListView(LoginRequiredMixin, UsuarioGestaoPermissaoMixin, ListView):
	template_name = 'usuarios_lista.html'
	model = User
	context_object_name = 'usuarios'
	paginate_by = 15

	def get_queryset(self):
		queryset = super().get_queryset()
		busca = self.request.GET.get('q', '').strip()
		if busca:
			queryset = queryset.filter(
				Q(username__icontains=busca)
				| Q(first_name__icontains=busca)
				| Q(last_name__icontains=busca)
				| Q(email__icontains=busca)
			)
		return queryset.order_by('username')

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		query_params = self.request.GET.copy()
		query_params.pop('page', None)
		context['filtro_query'] = query_params.urlencode()
		context['busca'] = self.request.GET.get('q', '').strip()
		context['total_filtrado'] = context['paginator'].count
		return context


class UsuarioCreateView(LoginRequiredMixin, UsuarioGestaoPermissaoMixin, CreateView):
	template_name = 'usuarios_form.html'
	form_class = UsuarioCreateForm
	success_url = reverse_lazy('usuarios-lista')

	def form_valid(self, form):
		response = super().form_valid(form)
		messages.success(self.request, 'Usuario criado com sucesso.')
		return response

	def form_invalid(self, form):
		messages.error(self.request, 'Nao foi possivel criar o usuario. Verifique os campos.')
		return super().form_invalid(form)


class UsuarioUpdateView(LoginRequiredMixin, UsuarioGestaoPermissaoMixin, UpdateView):
	template_name = 'usuarios_form.html'
	model = User
	form_class = UsuarioUpdateForm
	success_url = reverse_lazy('usuarios-lista')

	def form_valid(self, form):
		response = super().form_valid(form)
		messages.success(self.request, 'Usuario atualizado com sucesso.')
		return response

	def form_invalid(self, form):
		messages.error(self.request, 'Nao foi possivel atualizar o usuario. Verifique os campos.')
		return super().form_invalid(form)


class UsuarioDeleteView(LoginRequiredMixin, UsuarioGestaoPermissaoMixin, DeleteView):
	template_name = 'usuario_confirm_delete.html'
	model = User
	context_object_name = 'usuario_obj'
	success_url = reverse_lazy('usuarios-lista')

	def delete(self, request, *args, **kwargs):
		messages.success(request, 'Usuario excluido com sucesso.')
		return super().delete(request, *args, **kwargs)

def forbidden_view(request, exception=None):
	return render(request, '403.html', status=403)


def not_found_view(request, exception=None):
	return render(request, '404.html', status=404)
