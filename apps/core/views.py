import json
import logging

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db import connection
from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from apps.acolhimento import template_config
from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato, TemplateWhatsapp
from apps.acolhimento.whatsapp_rules import contar_pessoas_janela_aberta
from apps.core.forms import PerfilForm, QrCodeDinamicoForm, UsuarioCreateForm, UsuarioUpdateForm
from apps.core.models import QrCodeDinamico
from apps.core.qrcode_utils import gerar_qrcode_png, gerar_qrcode_svg


User = get_user_model()

logger = logging.getLogger(__name__)


def healthz(request):
	"""Healthcheck leve para proxy/monitor: 200 se o banco responde, 503 se nao.

	Publico e sem estado (nao exige login). Faz um SELECT 1 para garantir que a
	conexao com o banco esta viva (pega o caso 'app no ar, banco fora').
	"""
	try:
		with connection.cursor() as cursor:
			cursor.execute('SELECT 1')
			cursor.fetchone()
	except Exception:
		logger.exception('Healthcheck falhou: banco inacessivel.')
		return JsonResponse({'status': 'error', 'database': 'down'}, status=503)
	return JsonResponse({'status': 'ok'}, status=200)


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


# ---------------------------------------------------------------------------
# QR Codes dinamicos (link fixo + destino reconfiguravel)
# ---------------------------------------------------------------------------

class QrCodeAdminMixin(LoginRequiredMixin, UsuarioGestaoPermissaoMixin):
	"""Restringe a administracao de QR Codes a superusuarios (admins)."""

	model = QrCodeDinamico


class QrCodeListView(QrCodeAdminMixin, ListView):
	template_name = 'qrcodes_lista.html'
	context_object_name = 'qrcodes'
	paginate_by = 20

	def get_queryset(self):
		queryset = super().get_queryset()
		busca = self.request.GET.get('q', '').strip()
		if busca:
			queryset = queryset.filter(
				Q(nome__icontains=busca)
				| Q(destino__icontains=busca)
				| Q(codigo__icontains=busca)
			)
		return queryset

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		query_params = self.request.GET.copy()
		query_params.pop('page', None)
		context['filtro_query'] = query_params.urlencode()
		context['busca'] = self.request.GET.get('q', '').strip()
		context['total_filtrado'] = context['paginator'].count
		return context


class QrCodeCreateView(QrCodeAdminMixin, CreateView):
	template_name = 'qrcode_form.html'
	form_class = QrCodeDinamicoForm

	def form_valid(self, form):
		form.instance.criado_por = self.request.user
		response = super().form_valid(form)
		messages.success(self.request, 'QR Code criado com sucesso. Agora e so imprimir.')
		return response

	def form_invalid(self, form):
		messages.error(self.request, 'Nao foi possivel salvar o QR Code. Verifique os campos.')
		return super().form_invalid(form)

	def get_success_url(self):
		return reverse('qrcodes-detalhe', args=[self.object.pk])


class QrCodeUpdateView(QrCodeAdminMixin, UpdateView):
	template_name = 'qrcode_form.html'
	form_class = QrCodeDinamicoForm

	def form_valid(self, form):
		response = super().form_valid(form)
		messages.success(self.request, 'QR Code atualizado com sucesso.')
		return response

	def form_invalid(self, form):
		messages.error(self.request, 'Nao foi possivel salvar o QR Code. Verifique os campos.')
		return super().form_invalid(form)

	def get_success_url(self):
		return reverse('qrcodes-detalhe', args=[self.object.pk])


class QrCodeDeleteView(QrCodeAdminMixin, DeleteView):
	template_name = 'qrcode_confirm_delete.html'
	context_object_name = 'qrcode'
	success_url = reverse_lazy('qrcodes-lista')

	def form_valid(self, form):
		messages.success(self.request, 'QR Code excluido com sucesso.')
		return super().form_valid(form)


class QrCodeDetailView(QrCodeAdminMixin, DetailView):
	template_name = 'qrcode_detalhe.html'
	context_object_name = 'qrcode'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		link_publico = self.request.build_absolute_uri(self.object.get_public_path())
		context['link_publico'] = link_publico
		context['qrcode_svg'] = gerar_qrcode_svg(link_publico)
		return context


class QrCodeImprimirView(QrCodeAdminMixin, DetailView):
	template_name = 'qrcode_imprimir.html'
	context_object_name = 'qrcode'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		link_publico = self.request.build_absolute_uri(self.object.get_public_path())
		context['link_publico'] = link_publico
		context['qrcode_svg'] = gerar_qrcode_svg(link_publico)
		return context


class QrCodePngView(QrCodeAdminMixin, DetailView):
	"""Download do QR Code em PNG (bom para colar em cartazes/flyers)."""

	def get(self, request, *args, **kwargs):
		self.object = self.get_object()
		link_publico = request.build_absolute_uri(self.object.get_public_path())
		png = gerar_qrcode_png(link_publico)
		response = HttpResponse(png, content_type='image/png')
		response['Content-Disposition'] = f'attachment; filename="qrcode-{self.object.codigo}.png"'
		return response


def qr_redirect(request, codigo):
	"""Endpoint publico do QR Code: consulta o destino atual e redireciona (302).

	Publico e sem login: e a URL fixa que fica impressa dentro do QR Code.
	"""
	qrcode_obj = get_object_or_404(QrCodeDinamico, codigo=codigo)
	if not qrcode_obj.ativo:
		return render(request, 'qrcode_inativo.html', status=404)

	# F() evita corrida de contagem quando varias pessoas escaneiam ao mesmo tempo.
	QrCodeDinamico.objects.filter(pk=qrcode_obj.pk).update(
		total_acessos=F('total_acessos') + 1,
		ultimo_acesso=timezone.now(),
	)
	response = redirect(qrcode_obj.destino)
	# Sem cache no redirect: trocar o destino tem efeito imediato.
	response['Cache-Control'] = 'no-store, max-age=0'
	return response
