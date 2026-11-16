import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db import connection
from django.db.models import Exists, F, OuterRef, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from apps.acolhimento import atencao, boas_vindas, template_config
from apps.acolhimento.models import ConfiguracaoProcessamentoFila, InteracaoAcolhimento, MensagemContato, PrimeiroContato, RegistroGabinete, TemplateWhatsapp
from apps.acolhimento.whatsapp_rules import contar_pessoas_janela_aberta
from apps.core.forms import PerfilForm, QrCodeDinamicoForm, UsuarioCreateForm, UsuarioUpdateForm, eh_pastor
from apps.core.models import QrCodeDinamico, telefone_do_usuario
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


class StaffOuSuperuserPermissaoMixin(UserPassesTestMixin):
	"""Libera a tela para staff ou superusuario (admins)."""

	raise_exception = True

	def test_func(self):
		return self.request.user.is_staff or self.request.user.is_superuser


class DashboardView(LoginRequiredMixin, TemplateView):
	template_name = 'dashboard.html'

	def get_pessoas_boas_vindas_queryset(self):
		# Nao manda boas-vindas para quem JA iniciou a conversa (o atendimento reativo
		# do bot ja assume) nem antes da carencia HORAS_ESPERA_OPTIN. Ver boas_vindas.
		return boas_vindas.elegiveis()

	def post(self, request, *args, **kwargs):
		if not (request.user.is_staff or request.user.is_superuser):
			messages.error(request, 'Voce nao tem permissao para executar este disparo.')
			return redirect('dashboard')

		action = request.POST.get('action', '').strip()
		if action == 'boas_vindas_auto':
			return self._alternar_boas_vindas_auto(request)
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

		if not boas_vindas.configurado():
			messages.error(request, template_config.mensagem_nao_configurado(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO))
			return redirect('dashboard')

		mensagens = boas_vindas.enfileirar(pessoas_primeiro_contato, request.user)
		messages.success(
			request,
			f'Disparo de boas-vindas criado para {len(mensagens)} pessoa(s) e status atualizado para Visitante.',
		)
		return redirect('dashboard')

	@staticmethod
	def _alternar_boas_vindas_auto(request):
		cfg = ConfiguracaoProcessamentoFila.carregar()
		cfg.boas_vindas_auto = not cfg.boas_vindas_auto
		cfg.atualizado_por = request.user
		cfg.save(update_fields=['boas_vindas_auto', 'atualizado_por', 'atualizado_em'])
		if not cfg.boas_vindas_auto:
			messages.info(request, 'Boas-vindas automaticas DESLIGADAS: o envio volta a ser pelo botao "Disparar boas-vindas".')
			return redirect('dashboard')
		aviso = 'Boas-vindas automaticas LIGADAS: cada pessoa cadastrada ja recebe a mensagem de boas-vindas.'
		pendentes = boas_vindas.elegiveis().count()
		if pendentes:
			aviso += (
				f' Quem ja estava esperando ({pendentes} pessoa{"s" if pendentes != 1 else ""}) nao recebe sozinho:'
				' use o botao "Disparar boas-vindas" para essas.'
			)
		messages.success(request, aviso)
		if not boas_vindas.configurado():
			messages.warning(request, template_config.mensagem_nao_configurado(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO))
		return redirect('dashboard')

	LIMITE_PAINEL = 8

	@classmethod
	def contexto_atencao(cls, usuario):
		"""Blocos "Precisam de atencao" (a equipe toda) e "Meus acolhidos" (do usuario)."""
		agora = timezone.now()
		atrasados = atencao.precisam_de_atencao().select_related('responsavel_atual')
		atencao_total = atrasados.count()
		atencao_lista = atencao.marcar(list(atrasados[:cls.LIMITE_PAINEL]), agora)

		novo_retorno = MensagemContato.objects.filter(
			pessoa=OuterRef('pk'),
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			visualizada_equipe_em__isnull=True,
		)
		meus = atencao.anotar_ultimo_contato(
			PrimeiroContato.objects.filter(responsavel_atual=usuario, status__in=atencao.EM_ACOLHIMENTO)
		).annotate(tem_novo_retorno=Exists(novo_retorno))
		meus = atencao.marcar(list(meus), agora)
		# Quem escreveu e ninguem viu vem primeiro; depois os atrasados; depois o resto,
		# sempre do contato mais antigo para o mais recente.
		meus.sort(key=lambda p: (not p.tem_novo_retorno, not p.atrasada, p.ultimo_contato))
		return {
			'atencao_total': atencao_total,
			'atencao_lista': atencao_lista,
			'atencao_prazos': [
				(PrimeiroContato.StatusAcolhimento(status).label, dias) for status, dias in atencao.PRAZO_DIAS.items()
			],
			'meus_acolhidos': meus[:cls.LIMITE_PAINEL * 2],
			'meus_acolhidos_total': len(meus),
			'meus_acolhidos_atrasados': sum(1 for p in meus if p.atrasada),
		}

	@staticmethod
	def gabinetes_do_pastor(pastor):
		"""Pessoas com gabinete atribuido ao pastor: proximos agendados primeiro, depois os demais.

		Cada item: {'pessoa', 'proximo' (agendado de hoje em diante), 'ultimo' (ate hoje)}.
		"""
		hoje = timezone.localdate()
		registros = (
			RegistroGabinete.objects.filter(pastor=pastor)
			.select_related('pessoa')
			.order_by('data', 'hora', 'id')
		)
		por_pessoa = {}
		for registro in registros:
			item = por_pessoa.setdefault(registro.pessoa_id, {'pessoa': registro.pessoa, 'proximo': None, 'ultimo': None})
			if registro.data >= hoje and registro.tipo == RegistroGabinete.Tipo.AGENDADO:
				if item['proximo'] is None:
					item['proximo'] = registro
			else:
				item['ultimo'] = registro
		com_proximo = sorted(
			(item for item in por_pessoa.values() if item['proximo']),
			key=lambda item: (item['proximo'].data, item['proximo'].hora),
		)
		sem_proximo = sorted(
			(item for item in por_pessoa.values() if not item['proximo'] and item['ultimo']),
			key=lambda item: (item['ultimo'].data, item['ultimo'].hora),
			reverse=True,
		)
		return com_proximo + sem_proximo

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)

		total_pessoas = PrimeiroContato.objects.count()
		# KPI do card = todo mundo no status (bate com o filtro ?status=primeiro_contato).
		# O badge do botao usa total_boas_vindas, que e o subconjunto elegivel ao disparo.
		total_primeiro_contato = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO
		).count()
		total_boas_vindas = self.get_pessoas_boas_vindas_queryset().count()
		total_robo = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.ROBO
		).count()
		total_acompanhamento = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
		).count()
		total_participante = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.PARTICIPANTE
		).count()
		total_em_gabinete = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.EM_GABINETE
		).count()
		total_aprovado = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.APROVADO
		).count()
		total_membros = PrimeiroContato.objects.filter(
			status=PrimeiroContato.StatusAcolhimento.MEMBRO
		).count()
		total_janela_aberta = contar_pessoas_janela_aberta()

		ultimos_passos = InteracaoAcolhimento.objects.select_related('pessoa')[:8]
		if eh_pastor(self.request.user):
			context['gabinetes_do_pastor'] = self.gabinetes_do_pastor(self.request.user)
		context.update(self.contexto_atencao(self.request.user))

		context.update(
			{
				'total_pessoas': total_pessoas,
				'total_primeiro_contato': total_primeiro_contato,
				'total_boas_vindas': total_boas_vindas,
				'boas_vindas_auto': boas_vindas.automatico_ligado(),
				'total_robo': total_robo,
				'total_acompanhamento': total_acompanhamento,
				'total_participante': total_participante,
				'total_em_gabinete': total_em_gabinete,
				'total_aprovado': total_aprovado,
				'total_membros': total_membros,
				'total_janela_aberta': total_janela_aberta,
				'ultimos_passos': ultimos_passos,
				# Aviso no topo do Painel ate o usuario cadastrar o proprio telefone.
				'telefone_pendente': not telefone_do_usuario(self.request.user),
			}
		)

		return context


class PerfilView(LoginRequiredMixin, TemplateView):
	template_name = 'perfil.html'

	def destino_seguro(self):
		"""`?next=` de quem veio de outra tela (ex.: aviso do Painel), so se for do proprio site."""
		destino = (self.request.POST.get('next') or self.request.GET.get('next') or '').strip()
		if destino and url_has_allowed_host_and_scheme(
			destino, allowed_hosts={self.request.get_host()}, require_https=self.request.is_secure()
		):
			return destino
		return ''

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context['usuario_obj'] = self.request.user
		context['eh_pastor'] = eh_pastor(self.request.user)
		context['telefone_atual'] = telefone_do_usuario(self.request.user)
		context['next'] = self.destino_seguro()
		perfil_form = kwargs.get('perfil_form') or PerfilForm(instance=self.request.user)
		if context['next'] and not context['telefone_atual']:
			# Veio do aviso do Painel: o cursor ja abre no campo de telefone.
			perfil_form.fields['telefone'].widget.attrs['autofocus'] = True
		context['perfil_form'] = perfil_form
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
				return redirect(self.destino_seguro() or 'perfil')
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
		pastor = User.user_permissions.through.objects.filter(
			user_id=OuterRef('pk'),
			permission__codename='pode_ver_gabinete',
			permission__content_type__app_label='acolhimento',
		)
		return queryset.annotate(
			eh_pastor=Exists(pastor),
			telefone=F('perfil__telefone'),
		).order_by('username')

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
	# Sem isto o Django chamaria o usuario editado de `user` e o topo/menu passariam a
	# mostrar o nome e as permissoes dele em vez das de quem esta logado.
	context_object_name = 'usuario_obj'
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

class QrCodeAdminMixin(LoginRequiredMixin, StaffOuSuperuserPermissaoMixin):
	"""Restringe a administracao de QR Codes a staff ou superusuarios (admins)."""

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
