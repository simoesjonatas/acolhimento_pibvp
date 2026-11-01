import csv
import json
import logging
import re
import threading
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.http import HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db import transaction
from django.db.models import Count, Exists, F, Max, OuterRef, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, FormView, ListView, TemplateView, UpdateView

from apps.acolhimento import fila_auto, template_config
from apps.acolhimento.aguardando_resposta import (
	DIAS_PESSOA_NOVA,
	DIAS_RESPONDIDAS,
	ORIGEM_DISPARO_MASSA,
	anotar_conversa,
	corte_pessoa_nova,
	detalhar_aguardando,
	detalhar_respondidas,
	filtrar_aguardando,
	filtrar_respondidas,
)
from apps.acolhimento.fila_metrics import metricas_entrega
from apps.acolhimento.forms import AutoCadastroPrimeiroContatoForm, DisparoMensagemMassaForm, EnfileirarMensagemForm, InteracaoAcolhimentoForm, PerguntaForm, PrimeiroContatoForm, QuestionarioForm, RegistroGabineteForm, RelatorioPessoasForm, ResponderQuestionarioForm, TemplatesWhatsappForm
from apps.acolhimento.models import ConfiguracaoProcessamentoFila, ConviteQuestionario, ExecucaoProcessamentoFila, InteracaoAcolhimento, MensagemContato, PerguntaQuestionario, PrimeiroContato, Questionario, RespostaPergunta, TemplateWhatsapp
from apps.acolhimento.phone_utils import build_auto_nome_from_phone, find_pessoa_by_phone, phone_for_cadastro
from apps.acolhimento.reports import filtrar_pessoas, gerar_exportacao_respostas, gerar_relatorio, resumo_filtros, resumo_pessoas
from apps.acolhimento.whatsapp_rules import (
	JANELA_ATENDIMENTO_HORAS,
	WHATSAPP_OPTIN_REQUIRED_ERROR,
	filtrar_janela_aberta,
	filtrar_janela_fechada,
	janela_atendimento_aberta,
	motivo_bloqueio_livre,
	motivo_bloqueio_template_continuar,
	pessoa_pode_receber_whatsapp,
	pode_enviar_livre,
	pode_enviar_template_continuar,
	precisa_template_continuar,
	proximo_template_continuar_em,
	ultima_entrada_em,
)


PERMISSAO_CONVERSAR_PESSOAS = 'acolhimento.pode_conversar_pessoas'
PERMISSAO_GABINETE = 'acolhimento.pode_ver_gabinete'

logger = logging.getLogger(__name__)


def pode_conversar_pessoas(user):
	return user.is_staff or user.is_superuser or user.has_perm(PERMISSAO_CONVERSAR_PESSOAS)


def pode_ver_questionario(user):
	return pode_conversar_pessoas(user)


def redirect_ficha(pessoa_pk, aba=None):
	"""Volta para a ficha da pessoa ja na aba onde a acao aconteceu."""
	url = reverse('pessoas-detalhe', kwargs={'pk': pessoa_pk})
	return redirect(f'{url}?aba={aba}' if aba else url)


def pode_ver_gabinete(user):
	"""Gabinete pastoral: so administradores e quem tem o perfil Pastor."""
	return user.is_staff or user.is_superuser or user.has_perm(PERMISSAO_GABINETE)


CONVITE_SO_PARTICIPANTE_ERROR = (
	'O questionario de membresia so pode ser gerado quando a pessoa esta no status Ingressante.'
)


def eh_questionario_membresia(questionario):
	membresia = Questionario.membresia()
	return membresia is not None and membresia.pk == questionario.pk


def motivo_bloqueio_convite(pessoa, questionario):
	"""Mensagem de bloqueio para gerar o link do questionario para a pessoa, ou None.

	So o questionario de membresia depende do status (ele nasce quando a pessoa vira
	Participante). Os demais podem ir para qualquer pessoa, ex.: o convite do cafe com o
	pastor para quem esta em acompanhamento.
	"""
	if pessoa.status != PrimeiroContato.StatusAcolhimento.PARTICIPANTE and eh_questionario_membresia(questionario):
		return CONVITE_SO_PARTICIPANTE_ERROR
	return None


def convite_membresia(pessoa, usuario):
	"""Garante o convite do questionario de membresia da pessoa.

	Retorna `(convite, criado)`. Reaproveita um convite pendente em vez de duplicar
	links e devolve `(None, False)` quando nao ha questionario de membresia ativo
	com perguntas.
	"""
	questionario = Questionario.membresia()
	if questionario is None or not questionario.perguntas.exists():
		return None, False

	convites = ConviteQuestionario.objects.filter(pessoa=pessoa, questionario=questionario)
	existente = (
		convites.filter(status=ConviteQuestionario.StatusConvite.PENDENTE).first()
		or convites.first()
	)
	if existente is not None:
		return existente, False

	with transaction.atomic():
		convite = ConviteQuestionario.objects.create(
			questionario=questionario,
			pessoa=pessoa,
			criado_por=usuario,
		)
		InteracaoAcolhimento.objects.create(
			pessoa=pessoa,
			tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
			descricao=f'Link do questionario "{questionario.titulo}" gerado para preenchimento.',
		)
	return convite, True


class MensagensPermissaoMixin(UserPassesTestMixin):
	raise_exception = True

	def test_func(self):
		return self.request.user.is_staff or self.request.user.is_superuser


class AdminPermissaoMixin(UserPassesTestMixin):
	"""Restringe a view a administradores (staff/superuser).

	Sem `raise_exception`: usuario anonimo e redirecionado para o login e
	usuario logado sem permissao recebe 403.
	"""

	def test_func(self):
		user = self.request.user
		return user.is_staff or user.is_superuser


class SuperAdminPermissaoMixin(UserPassesTestMixin):
	"""Restringe a view a administradores superuser."""

	def test_func(self):
		return self.request.user.is_superuser


class ConversasPessoasPermissaoMixin(UserPassesTestMixin):
	raise_exception = True

	def test_func(self):
		return pode_conversar_pessoas(self.request.user)


class GabinetePermissaoMixin(UserPassesTestMixin):
	raise_exception = True

	def test_func(self):
		return pode_ver_gabinete(self.request.user)


class PrimeiroContatoQuerysetMixin:
	sort_map = {
		'nome': 'nome',
		'telefone': 'telefone_whatsapp',
		'email': 'email',
		'status': 'status',
		'data': 'data_primeiro_contato',
	}

	def get_sort_state(self):
		sort_coluna = self.request.GET.get('sort', 'data').strip()
		direcao = self.request.GET.get('direcao', 'desc').strip().lower()

		if sort_coluna not in self.sort_map:
			sort_coluna = 'data'
		if direcao not in ('asc', 'desc'):
			direcao = 'desc'

		return sort_coluna, direcao

	def get_filtered_queryset(self, queryset):
		busca = self.request.GET.get('q', '').strip()
		status = self.request.GET.get('status', '').strip()
		origem = self.request.GET.get('origem', '').strip()
		iniciou_interacao = self.request.GET.get('iniciou_interacao', '').strip()
		janela = self.request.GET.get('janela', '').strip()
		meus = self.request.GET.get('meus', '').strip()
		sort_coluna, direcao = self.get_sort_state()

		if meus == '1':
			queryset = queryset.filter(responsavel_atual=self.request.user)

		if busca:
			queryset = queryset.filter(
				Q(nome__icontains=busca)
				| Q(telefone_whatsapp__icontains=busca)
				| Q(email__icontains=busca)
				| Q(religiao__icontains=busca)
			)

		if status in PrimeiroContato.StatusAcolhimento.values:
			queryset = queryset.filter(status=status)

		if origem:
			queryset = queryset.filter(origem_cadastro=origem)

		if iniciou_interacao == 'sim':
			queryset = queryset.filter(iniciou_interacao=True)
		elif iniciou_interacao == 'nao':
			queryset = queryset.filter(iniciou_interacao=False)

		if janela == 'aberta':
			queryset = filtrar_janela_aberta(queryset)
		elif janela == 'fechada':
			queryset = filtrar_janela_fechada(queryset)

		ordering = self.sort_map.get(sort_coluna, 'data_primeiro_contato')
		if direcao == 'desc':
			ordering = f'-{ordering}'

		queryset = queryset.order_by(ordering)
		return queryset


class PrimeiroContatoListView(LoginRequiredMixin, PrimeiroContatoQuerysetMixin, ListView):
	template_name = 'pessoas.html'
	model = PrimeiroContato
	context_object_name = 'pessoas'
	paginate_by = 10

	def get_queryset(self):
		queryset = super().get_queryset()
		queryset = self.get_filtered_queryset(queryset)
		retorno_pendente_subquery = MensagemContato.objects.filter(
			pessoa=OuterRef('pk'),
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			visualizada_equipe_em__isnull=True,
		)
		queryset = queryset.annotate(has_novo_retorno=Exists(retorno_pendente_subquery))

		return queryset

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		query_params = self.request.GET.copy()
		query_params.pop('page', None)
		filtro_query = query_params.urlencode()

		sort_coluna_atual, sort_direcao_atual = self.get_sort_state()
		base_sort_params = self.request.GET.copy()
		base_sort_params.pop('page', None)
		base_sort_params.pop('sort', None)
		base_sort_params.pop('direcao', None)

		sort_links = {}
		for coluna in self.sort_map:
			params = base_sort_params.copy()
			next_direcao = 'asc'
			if coluna == sort_coluna_atual and sort_direcao_atual == 'asc':
				next_direcao = 'desc'
			params['sort'] = coluna
			params['direcao'] = next_direcao
			sort_links[coluna] = params.urlencode()

		context['busca'] = self.request.GET.get('q', '').strip()
		context['status_atual'] = self.request.GET.get('status', '').strip()
		context['origem_atual'] = self.request.GET.get('origem', '').strip()
		context['iniciou_interacao_atual'] = self.request.GET.get('iniciou_interacao', '').strip()
		context['janela_atual'] = self.request.GET.get('janela', '').strip()
		context['meus_atual'] = self.request.GET.get('meus', '').strip()
		context['status_choices'] = PrimeiroContato.StatusAcolhimento.choices
		context['origem_choices'] = PrimeiroContato.OrigemCadastroChoices.choices
		context['sort_coluna_atual'] = sort_coluna_atual
		context['sort_direcao_atual'] = sort_direcao_atual
		context['sort_links'] = sort_links
		context['total_filtrado'] = context['paginator'].count
		context['filtro_query'] = filtro_query
		return context


class MensagemFilaQuerysetMixin:
	sort_map = {
		'pessoa': 'pessoa__nome',
		'status': 'status_fila',
		'canal': 'canal',
		'direcao': 'direcao',
		'prioridade': 'prioridade',
		'enfileirada': 'enfileirada_em',
		'enviada': 'enviada_em',
	}

	def get_sort_state(self):
		sort_coluna = self.request.GET.get('sort', 'enfileirada').strip()
		direcao = self.request.GET.get('direcao', 'desc').strip().lower()

		if sort_coluna not in self.sort_map:
			sort_coluna = 'enfileirada'
		if direcao not in ('asc', 'desc'):
			direcao = 'desc'

		return sort_coluna, direcao

	def get_filtered_queryset(self, queryset, filtrar_situacao=True):
		"""Aplica os filtros da tela.

		Com `filtrar_situacao=False` ignora `status` e `resposta_status`, que sao os filtros
		que os cards do topo controlam: assim cada card conta o que a busca atual tem
		naquela situacao, e o numero bate com o que aparece ao clicar nele.
		"""
		busca = self.request.GET.get('q', '').strip()
		status = self.request.GET.get('status', '').strip() if filtrar_situacao else ''
		canal = self.request.GET.get('canal', '').strip()
		direcao_mensagem = self.request.GET.get('direcao_mensagem', '').strip()
		enviado = self.request.GET.get('enviado', '').strip()
		resposta_status = self.request.GET.get('resposta_status', '').strip() if filtrar_situacao else ''
		sort_coluna, direcao_ordenacao = self.get_sort_state()

		if busca:
			queryset = queryset.filter(
				Q(pessoa__nome__icontains=busca)
				| Q(pessoa__telefone_whatsapp__icontains=busca)
				| Q(conteudo__icontains=busca)
				| Q(referencia_externa__icontains=busca)
			)

		if status:
			queryset = queryset.filter(status_fila=status)

		if canal:
			queryset = queryset.filter(canal=canal)

		if direcao_mensagem:
			queryset = queryset.filter(direcao=direcao_mensagem)

		if enviado == 'sim':
			queryset = queryset.filter(enviada_em__isnull=False)
		elif enviado == 'nao':
			queryset = queryset.filter(enviada_em__isnull=True)

		if resposta_status == 'respondidas':
			queryset = queryset.filter(direcao='saida', resposta_recebida_em__isnull=False)
		elif resposta_status == 'sem_resposta':
			queryset = queryset.filter(direcao='saida', resposta_recebida_em__isnull=True)
		elif resposta_status == 'recebidas':
			queryset = queryset.filter(direcao='entrada')
		elif resposta_status == 'nao_lidas':
			# Mesmo criterio do contador do menu: recebida que ninguem da equipe abriu.
			queryset = queryset.filter(direcao='entrada', visualizada_equipe_em__isnull=True)

		ordering = self.sort_map.get(sort_coluna, 'enfileirada_em')
		if direcao_ordenacao == 'desc':
			ordering = f'-{ordering}'

		return queryset.order_by(ordering)


class MensagemFilaListView(LoginRequiredMixin, MensagensPermissaoMixin, MensagemFilaQuerysetMixin, ListView):
	template_name = 'mensagens_fila.html'
	model = MensagemContato
	context_object_name = 'mensagens'
	paginate_by = 20

	def get_queryset(self):
		queryset = super().get_queryset().select_related('pessoa', 'campanha', 'criado_por')
		return self.get_filtered_queryset(queryset)

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		query_params = self.request.GET.copy()
		query_params.pop('page', None)
		filtro_query = query_params.urlencode()
		fila_queryset = self.get_filtered_queryset(MensagemContato.objects.all(), filtrar_situacao=False)
		resumo_fila = fila_queryset.aggregate(
			pendentes=Count('id', filter=Q(status_fila=MensagemContato.StatusFilaChoices.PENDENTE)),
			falhas=Count('id', filter=Q(status_fila=MensagemContato.StatusFilaChoices.FALHA)),
			canceladas=Count('id', filter=Q(status_fila=MensagemContato.StatusFilaChoices.CANCELADA)),
			sem_resposta=Count(
				'id',
				filter=Q(
					direcao=MensagemContato.DirecaoChoices.SAIDA,
					resposta_recebida_em__isnull=True,
				),
			),
			recebidas=Count('id', filter=Q(direcao=MensagemContato.DirecaoChoices.ENTRADA)),
			nao_lidas=Count(
				'id',
				filter=Q(
					direcao=MensagemContato.DirecaoChoices.ENTRADA,
					visualizada_equipe_em__isnull=True,
				),
			),
		)

		sort_coluna_atual, sort_direcao_atual = self.get_sort_state()
		base_sort_params = self.request.GET.copy()
		base_sort_params.pop('page', None)
		base_sort_params.pop('sort', None)
		base_sort_params.pop('direcao', None)

		sort_links = {}
		for coluna in self.sort_map:
			params = base_sort_params.copy()
			next_direcao = 'asc'
			if coluna == sort_coluna_atual and sort_direcao_atual == 'asc':
				next_direcao = 'desc'
			params['sort'] = coluna
			params['direcao'] = next_direcao
			sort_links[coluna] = params.urlencode()

		context['busca'] = self.request.GET.get('q', '').strip()
		context['status_atual'] = self.request.GET.get('status', '').strip()
		context['canal_atual'] = self.request.GET.get('canal', '').strip()
		context['direcao_mensagem_atual'] = self.request.GET.get('direcao_mensagem', '').strip()
		context['enviado_atual'] = self.request.GET.get('enviado', '').strip()
		context['resposta_status_atual'] = self.request.GET.get('resposta_status', '').strip()
		context['status_choices'] = MensagemContato.StatusFilaChoices.choices
		context['canal_choices'] = MensagemContato.CanalChoices.choices
		context['direcao_choices'] = MensagemContato.DirecaoChoices.choices
		context['resposta_status_choices'] = [
			('respondidas', 'Respondidas'),
			('sem_resposta', 'Sem resposta'),
			('recebidas', 'Mensagens recebidas'),
			('nao_lidas', 'Recebidas não lidas'),
		]
		context['sort_coluna_atual'] = sort_coluna_atual
		context['sort_direcao_atual'] = sort_direcao_atual
		context['sort_links'] = sort_links
		context['total_filtrado'] = context['paginator'].count
		context['filtro_query'] = filtro_query

		# Cada card e um atalho que troca o par status/resposta_status e mantem o resto
		# (busca, canal, ordenacao). Clicar no card ja ativo desliga o filtro.
		card_params = self.request.GET.copy()
		for chave in ('page', 'status', 'resposta_status'):
			card_params.pop(chave, None)
		situacao_atual = (context['status_atual'], context['resposta_status_atual'])
		cards = [
			('Pendentes', 'pendentes', 'is-pending', MensagemContato.StatusFilaChoices.PENDENTE, ''),
			('Falhas', 'falhas', 'is-failed', MensagemContato.StatusFilaChoices.FALHA, ''),
			('Canceladas', 'canceladas', 'is-canceled', MensagemContato.StatusFilaChoices.CANCELADA, ''),
			('Sem resposta', 'sem_resposta', 'is-waiting', '', 'sem_resposta'),
			('Recebidas', 'recebidas', 'is-inbound', '', 'recebidas'),
			('Não lidas', 'nao_lidas', 'is-unread', '', 'nao_lidas'),
		]
		context['fila_resumo_cards'] = []
		for rotulo, chave, classe, status_card, resposta_card in cards:
			ativo = situacao_atual == (status_card, resposta_card)
			params = card_params.copy()
			if not ativo:
				if status_card:
					params['status'] = status_card
				if resposta_card:
					params['resposta_status'] = resposta_card
			context['fila_resumo_cards'].append({
				'rotulo': rotulo,
				'valor': resumo_fila[chave],
				'classe': classe,
				'ativo': ativo,
				'query': params.urlencode(),
			})
		return context


class ProcessamentoFilaControleView(LoginRequiredMixin, SuperAdminPermissaoMixin, TemplateView):
	template_name = 'mensagens_processamento.html'

	def post(self, request, *args, **kwargs):
		action = (request.POST.get('action') or '').strip()

		if action == 'iniciar':
			try:
				limite = max(int(request.POST.get('limit', '20')), 1)
			except ValueError:
				limite = 20

			dry_run = request.POST.get('dry_run') == 'on'
			execucao = fila_auto.iniciar_execucao_fila(
				solicitado_por=request.user,
				limite=limite,
				dry_run=dry_run,
				origem=ExecucaoProcessamentoFila.OrigemChoices.MANUAL,
			)
			if execucao is None:
				messages.error(request, 'Ja existe um processamento em execucao.')
			else:
				messages.success(request, f'Processamento #{execucao.id} iniciado.')
			return redirect('mensagens-processamento')

		if action in ('auto_on', 'auto_off'):
			cfg = ConfiguracaoProcessamentoFila.carregar()
			cfg.auto_ativo = (action == 'auto_on')
			cfg.atualizado_por = request.user
			cfg.save(update_fields=['auto_ativo', 'atualizado_por', 'atualizado_em'])
			if cfg.auto_ativo:
				fila_auto.disparar_auto_se_ligado()
				messages.success(request, 'Processamento automatico LIGADO: a fila e consumida sozinha a cada nova mensagem enfileirada.')
			else:
				messages.info(request, 'Processamento automatico DESLIGADO: as mensagens ficam na fila ate alguem processar manualmente.')
			return redirect('mensagens-processamento')

		if action == 'parar':
			execucao_id = request.POST.get('execucao_id', '').strip()
			execucao = ExecucaoProcessamentoFila.objects.filter(
				pk=execucao_id,
				status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO,
			).first()
			if not execucao:
				messages.error(request, 'Execucao nao encontrada ou ja finalizada.')
				return redirect('mensagens-processamento')

			execucao.solicitar_parada = True
			execucao.save(update_fields=['solicitar_parada', 'atualizado_em'])
			messages.info(request, f'Parada solicitada para execucao #{execucao.id}.')
			return redirect('mensagens-processamento')

		messages.error(request, 'Acao invalida.')
		return redirect('mensagens-processamento')

	@staticmethod
	def _duracao_texto(execucao):
		if not (execucao.iniciado_em and execucao.finalizado_em):
			return None
		total = int((execucao.finalizado_em - execucao.iniciado_em).total_seconds())
		if total < 60:
			return f'{total}s'
		minutos, segundos = divmod(total, 60)
		if minutos < 60:
			return f'{minutos}min {segundos}s'
		horas, minutos = divmod(minutos, 60)
		return f'{horas}h {minutos}min'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		# Destrava execucoes orfas (processo que morreu no meio) ao abrir a tela.
		fila_auto.recuperar_execucoes_presas()
		execucoes_queryset = ExecucaoProcessamentoFila.objects.select_related('solicitado_por')
		paginator = Paginator(execucoes_queryset, 8)
		page_obj = paginator.get_page(self.request.GET.get('page'))

		execucoes = list(page_obj.object_list)
		for execucao in execucoes:
			execucao.duracao_texto = self._duracao_texto(execucao)
			execucao.log_linhas = len(execucao.log_execucao.strip().splitlines()) if execucao.log_execucao else 0

		execucao_ativa = execucoes_queryset.filter(
			status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO
		).first()
		if execucao_ativa:
			linhas = execucao_ativa.log_execucao.strip().splitlines() if execucao_ativa.log_execucao else []
			execucao_ativa.log_tail = '\n'.join(linhas[-8:])

		status_fila = MensagemContato.StatusFilaChoices
		fila = MensagemContato.objects.filter(
			direcao=MensagemContato.DirecaoChoices.SAIDA
		).aggregate(
			pendentes=Count('id', filter=Q(status_fila=status_fila.PENDENTE)),
			agendadas=Count('id', filter=Q(status_fila=status_fila.AGENDADA)),
			processando=Count('id', filter=Q(status_fila=status_fila.PROCESSANDO)),
			falhas=Count('id', filter=Q(status_fila=status_fila.FALHA)),
			enviadas_hoje=Count(
				'id',
				filter=Q(status_fila=status_fila.ENVIADA, enviada_em__date=timezone.localdate()),
			),
		)

		context['execucoes'] = execucoes
		context['page_obj'] = page_obj
		context['paginator'] = paginator
		context['is_paginated'] = paginator.num_pages > 1
		context['execucao_ativa'] = execucao_ativa
		context['total_execucoes'] = paginator.count
		context['fila'] = fila
		context['entrega'] = metricas_entrega()
		context['auto_ativo'] = ConfiguracaoProcessamentoFila.auto_ligado()
		return context


class DisparoMensagemMassaView(LoginRequiredMixin, MensagensPermissaoMixin, FormView):
	template_name = 'mensagens_disparo_massa.html'
	form_class = DisparoMensagemMassaForm
	success_url = reverse_lazy('mensagens-disparo-massa')

	def get_pessoas_queryset(self):
		queryset = PrimeiroContato.objects.select_related('responsavel_atual').exclude(
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO
		)

		busca = self.request.GET.get('q', '').strip()
		status = self.request.GET.get('status', '').strip()
		origem = self.request.GET.get('origem', '').strip()
		responsavel = self.request.GET.get('responsavel', '').strip()
		sem_pendente = self.request.GET.get('sem_pendente', '').strip()

		if busca:
			queryset = queryset.filter(
				Q(nome__icontains=busca)
				| Q(telefone_whatsapp__icontains=busca)
				| Q(email__icontains=busca)
			)

		if status:
			queryset = queryset.filter(status=status)

		if origem:
			queryset = queryset.filter(origem_cadastro=origem)

		if responsavel == 'sem_responsavel':
			queryset = queryset.filter(responsavel_atual__isnull=True)
		elif responsavel:
			queryset = queryset.filter(responsavel_atual_id=responsavel)

		if sem_pendente == 'sim':
			queryset = queryset.exclude(mensagens__status_fila=MensagemContato.StatusFilaChoices.PENDENTE)

		return (
			queryset.annotate(
				ultima_entrada=Max(
					'mensagens__enfileirada_em',
					filter=Q(mensagens__direcao=MensagemContato.DirecaoChoices.ENTRADA),
				)
			)
			.order_by('nome')
			.distinct()
		)

	def get_form_kwargs(self):
		kwargs = super().get_form_kwargs()
		kwargs['pessoas_queryset'] = self.get_pessoas_queryset()
		return kwargs

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		queryset_filtrado = self.get_pessoas_queryset()
		responsaveis_raw = (
			PrimeiroContato.objects.exclude(responsavel_atual__isnull=True)
			.values_list('responsavel_atual_id', 'responsavel_atual__first_name', 'responsavel_atual__last_name', 'responsavel_atual__username')
			.distinct()
		)
		responsaveis_choices = []
		for responsavel_id, first_name, last_name, username in responsaveis_raw:
			nome_completo = f'{first_name or ""} {last_name or ""}'.strip()
			label = nome_completo or username
			responsaveis_choices.append((responsavel_id, label))

		selected_pessoas = []
		if self.request.method == 'POST':
			selected_pessoas = [
				int(valor)
				for valor in self.request.POST.getlist('pessoas')
				if valor.isdigit()
			]

		# Status da janela de 24h por pessoa (sem N+1: usa a anotacao ultima_entrada).
		limite_janela = timezone.now() - timedelta(hours=JANELA_ATENDIMENTO_HORAS)
		audiencia = []
		total_janela_aberta = 0
		for pessoa in queryset_filtrado:
			ultima = getattr(pessoa, 'ultima_entrada', None)
			janela_aberta = bool(ultima and ultima >= limite_janela)
			# TRAVA DESATIVADA (opt-in + janela de 24h) — ver apps/acolhimento/whatsapp_rules.py.
			# Para reativar, apague as duas linhas abaixo e descomente as originais.
			pode_livre = True
			if janela_aberta:
				total_janela_aberta += 1
			# pode_livre = bool(pessoa.iniciou_interacao and janela_aberta)
			# if pode_livre:
			# 	total_janela_aberta += 1
			audiencia.append({'pessoa': pessoa, 'janela_aberta': janela_aberta, 'pode_livre': pode_livre})

		context['busca'] = self.request.GET.get('q', '').strip()
		context['status_atual'] = self.request.GET.get('status', '').strip()
		context['origem_atual'] = self.request.GET.get('origem', '').strip()
		context['responsavel_atual_filtro'] = self.request.GET.get('responsavel', '').strip()
		context['sem_pendente_atual'] = self.request.GET.get('sem_pendente', '').strip()
		context['status_choices'] = [
			(valor, rotulo)
			for valor, rotulo in PrimeiroContato.StatusAcolhimento.choices
			if valor != PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO
		]
		context['origem_choices'] = PrimeiroContato.OrigemCadastroChoices.choices
		context['responsaveis_choices'] = responsaveis_choices
		context['audiencia'] = audiencia
		context['total_pessoas_filtradas'] = len(audiencia)
		context['total_janela_aberta'] = total_janela_aberta
		context['selected_pessoas'] = selected_pessoas
		context['usa_meta'] = template_config.usando_meta()
		context['usa_evolution'] = template_config.usando_evolution()
		context['modo_livre'] = DisparoMensagemMassaForm.MODO_LIVRE
		context['modo_template'] = DisparoMensagemMassaForm.MODO_TEMPLATE
		context['modo_questionario'] = DisparoMensagemMassaForm.MODO_QUESTIONARIO
		context['modo_atual'] = (
			self.request.POST.get('modo') if self.request.method == 'POST' else ''
		) or DisparoMensagemMassaForm.MODO_LIVRE
		return context

	def _enfileirar_template(self, form):
		"""Modo template: envia um template (marketing) para todos os selecionados.

		Na Twilio o template e o Content SID; na API oficial da Meta e o nome do
		template aprovado + idioma. O campo `content_sid` do formulario guarda os dois.
		"""
		pessoas = list(form.cleaned_data['pessoas'])
		content_sid = form.cleaned_data['content_sid'].strip()
		base_vars = form.cleaned_data.get('content_variables') or {}
		usa_meta = template_config.usando_meta()
		idioma = (form.cleaned_data.get('template_idioma') or '').strip() or 'pt_BR'

		def _variaveis_dict(pessoa):
			nome = (pessoa.nome or '').strip()
			return {
				str(chave): (valor.replace('{nome}', nome) if isinstance(valor, str) else valor)
				for chave, valor in base_vars.items()
			}

		def _variaveis_para(pessoa):
			if not base_vars:
				return ''
			return json.dumps(_variaveis_dict(pessoa), ensure_ascii=False)

		def _metadata_para(pessoa):
			if usa_meta:
				return {
					'tipo_template': 'marketing_massa',
					'meta_template': {
						'nome': content_sid,
						'idioma': idioma,
						'parametros': _variaveis_dict(pessoa),
					},
				}
			return {
				'tipo_template': 'marketing_massa',
				'twilio_template': {
					'content_sid': content_sid,
					'content_variables': _variaveis_para(pessoa),
				},
			}

		mensagens = [
			MensagemContato(
				pessoa=pessoa,
				criado_por=self.request.user,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
				status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
				prioridade=5,
				agendada_para=None,
				conteudo=f'[Template] {content_sid}',
				metadata_envio=_metadata_para(pessoa),
			)
			for pessoa in pessoas
		]
		MensagemContato.objects.bulk_create(mensagens)
		messages.success(self.request, f'{len(mensagens)} template(s) enfileirado(s) para disparo em massa.')
		return super().form_valid(form)

	def _enfileirar_livre(self, form):
		"""Modo livre: mensagem de texto so para quem esta com a janela de 24h aberta."""
		pessoas = list(form.cleaned_data['pessoas'])
		conteudo = form.cleaned_data['conteudo']

		pessoas_bloqueadas = [pessoa for pessoa in pessoas if not pode_enviar_livre(pessoa)]
		pessoas = [pessoa for pessoa in pessoas if pode_enviar_livre(pessoa)]

		if pessoas_bloqueadas:
			messages.warning(
				self.request,
				f'{len(pessoas_bloqueadas)} pessoa(s) ignorada(s): janela de 24h fechada. '
				'Use o disparo por template para alcanca-las.',
			)

		if not pessoas:
			messages.error(
				self.request,
				'Nenhuma mensagem livre foi criada: nenhum selecionado esta com a janela de 24h aberta.',
			)
			return redirect(self.success_url)

		mensagens = [
			MensagemContato(
				pessoa=pessoa,
				criado_por=self.request.user,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
				status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
				prioridade=5,
				agendada_para=None,
				conteudo=conteudo,
				# Envio em lote: nao conta como resposta na tela "Aguardando resposta".
				metadata_envio={'origem': ORIGEM_DISPARO_MASSA},
			)
			for pessoa in pessoas
		]
		MensagemContato.objects.bulk_create(mensagens)
		messages.success(self.request, f'{len(mensagens)} mensagem(ns) enfileirada(s) com sucesso.')
		return super().form_valid(form)

	def _texto_convite(self, texto, pessoa, questionario, convite):
		link = self.request.build_absolute_uri(
			reverse('responder-questionario', kwargs={'token': convite.token})
		)
		nome = (pessoa.nome or '').strip() or 'amigo(a)'
		return (
			texto.replace('{nome}', nome)
			.replace('{questionario}', questionario.titulo)
			.replace('{link}', link)
		)

	def _enfileirar_questionario(self, form):
		"""Modo questionario: cada pessoa recebe o proprio link de resposta.

		Reaproveita o link pendente de quem ja tem um e deixa de fora quem ja respondeu.
		O questionario de membresia so vai para quem esta como Participante.
		"""
		questionario = form.cleaned_data['questionario']
		texto = form.cleaned_data['texto_convite'].strip()
		pessoas = list(form.cleaned_data['pessoas'])
		ignoradas = []

		# O convite vai como texto livre: vale a mesma trava do modo livre.
		janela_fechada = [pessoa for pessoa in pessoas if not pode_enviar_livre(pessoa)]
		if janela_fechada:
			ignoradas.append(f'{len(janela_fechada)} com a janela de 24h fechada')
			pessoas = [pessoa for pessoa in pessoas if pode_enviar_livre(pessoa)]

		if eh_questionario_membresia(questionario):
			participantes = [
				pessoa for pessoa in pessoas
				if pessoa.status == PrimeiroContato.StatusAcolhimento.PARTICIPANTE
			]
			if len(participantes) < len(pessoas):
				ignoradas.append(
					f'{len(pessoas) - len(participantes)} fora do status Ingressante '
					'(o de membresia e so para eles)'
				)
				pessoas = participantes

		pendentes = {}
		respondidos = set()
		for convite in ConviteQuestionario.objects.filter(questionario=questionario, pessoa__in=pessoas):
			if convite.status == ConviteQuestionario.StatusConvite.PENDENTE:
				pendentes.setdefault(convite.pessoa_id, convite)
			else:
				respondidos.add(convite.pessoa_id)
		ja_responderam = respondidos - set(pendentes)
		if ja_responderam:
			ignoradas.append(f'{len(ja_responderam)} que ja responderam este questionario')
			pessoas = [pessoa for pessoa in pessoas if pessoa.pk not in ja_responderam]

		if not pessoas:
			messages.error(
				self.request,
				'Nenhum convite foi enfileirado: ' + '; '.join(ignoradas) + '.',
			)
			return redirect(self.success_url)

		with transaction.atomic():
			novos = ConviteQuestionario.objects.bulk_create([
				ConviteQuestionario(questionario=questionario, pessoa=pessoa, criado_por=self.request.user)
				for pessoa in pessoas
				if pessoa.pk not in pendentes
			])
			convites = {**pendentes, **{convite.pessoa_id: convite for convite in novos}}
			MensagemContato.objects.bulk_create([
				MensagemContato(
					pessoa=pessoa,
					criado_por=self.request.user,
					canal=MensagemContato.CanalChoices.WHATSAPP,
					direcao=MensagemContato.DirecaoChoices.SAIDA,
					status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
					prioridade=5,
					conteudo=self._texto_convite(texto, pessoa, questionario, convites[pessoa.pk]),
					# Envio em lote: nao conta como resposta na tela "Aguardando resposta".
					metadata_envio={
						'origem': ORIGEM_DISPARO_MASSA,
						'convite_questionario_id': convites[pessoa.pk].pk,
					},
				)
				for pessoa in pessoas
			])
			InteracaoAcolhimento.objects.bulk_create([
				InteracaoAcolhimento(
					pessoa=pessoa,
					tipo=InteracaoAcolhimento.TipoInteracao.TENTATIVA_CONTATO,
					descricao=(
						f'Link do questionario "{questionario.titulo}" enfileirado para envio '
						'pelo WhatsApp (disparo em massa).'
					),
				)
				for pessoa in pessoas
			])

		sucesso = (
			f'{len(pessoas)} convite(s) do questionario "{questionario.titulo}" enfileirado(s), '
			'cada um com o proprio link.'
		)
		if pendentes:
			sucesso += f' {len(pendentes)} reaproveitaram o link pendente que ja existia.'
		messages.success(self.request, sucesso)
		if ignoradas:
			messages.warning(self.request, 'Ficaram de fora: ' + '; '.join(ignoradas) + '.')
		return super().form_valid(form)

	def form_valid(self, form):
		modo = form.cleaned_data['modo']
		if modo == DisparoMensagemMassaForm.MODO_TEMPLATE:
			resposta = self._enfileirar_template(form)
		elif modo == DisparoMensagemMassaForm.MODO_QUESTIONARIO:
			resposta = self._enfileirar_questionario(form)
		else:
			resposta = self._enfileirar_livre(form)
		# Processamento automatico: bulk_create nao emite signal, entao dispara aqui.
		fila_auto.agendar_disparo_auto()
		return resposta


class PrimeiroContatoCreateView(LoginRequiredMixin, CreateView):
	template_name = 'pessoa_form.html'
	form_class = PrimeiroContatoForm
	success_url = reverse_lazy('pessoas-lista')

	def form_valid(self, form):
		form.instance.origem_cadastro = PrimeiroContato.OrigemCadastroChoices.EQUIPE
		form.instance.criado_por = self.request.user
		if not form.instance.responsavel_atual_id:
			form.instance.responsavel_atual = self.request.user
		response = super().form_valid(form)
		messages.success(self.request, 'Pessoa cadastrada com sucesso.')
		return response

	def form_invalid(self, form):
		messages.error(self.request, 'Nao foi possivel salvar. Verifique os campos e tente novamente.')
		return super().form_invalid(form)


class AutoCadastroCreateView(CreateView):
	template_name = 'auto_cadastro_form.html'
	form_class = AutoCadastroPrimeiroContatoForm
	success_url = reverse_lazy('auto-cadastro-sucesso')

	def form_valid(self, form):
		form.instance.origem_cadastro = PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO
		form.instance.criado_por = None
		return super().form_valid(form)


class AutoCadastroSuccessView(TemplateView):
	template_name = 'auto_cadastro_sucesso.html'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		# Opt-in-first (anti-bloqueio): oferece a PESSOA iniciar a conversa. Quando ela
		# manda a 1a mensagem, o webhook marca iniciou_interacao=True e o atendimento
		# reativo assume — sem cold outreach (o maior fator de bloqueio do WhatsApp).
		numero = re.sub(r'\D', '', getattr(settings, 'PIB_WHATSAPP_NUMERO_OPTIN', '') or '')
		if numero:
			texto = (getattr(settings, 'PIB_WHATSAPP_OPTIN_TEXTO', '') or '').strip()
			sufixo = f'?text={quote(texto)}' if texto else ''
			context['whatsapp_optin_url'] = f'https://wa.me/{numero}{sufixo}'
		return context


class PrimeiroContatoDetailView(LoginRequiredMixin, DetailView):
	template_name = 'pessoa_detail.html'
	model = PrimeiroContato
	context_object_name = 'pessoa'

	def get_timeline_queryset(self):
		queryset = self.object.interacoes.all()
		busca = self.request.GET.get('timeline_q', '').strip()
		tipo = self.request.GET.get('timeline_tipo', '').strip()
		ordem = self.request.GET.get('timeline_ordem', 'desc').strip()

		if busca:
			queryset = queryset.filter(
				Q(descricao__icontains=busca)
				| Q(tipo__icontains=busca)
			)

		tipos_validos = {choice[0] for choice in InteracaoAcolhimento.TipoInteracao.choices}
		if tipo in tipos_validos:
			queryset = queryset.filter(tipo=tipo)
		else:
			tipo = ''

		if ordem == 'asc':
			queryset = queryset.order_by('data_interacao', 'hora_interacao', 'criado_em')
		else:
			ordem = 'desc'
			queryset = queryset.order_by('-data_interacao', '-hora_interacao', '-criado_em')

		return queryset, {
			'timeline_q': busca,
			'timeline_tipo': tipo,
			'timeline_ordem': ordem,
		}

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		interacoes, timeline_filtros = self.get_timeline_queryset()
		context['interacoes'] = interacoes
		context['timeline_total'] = self.object.interacoes.count()
		context['timeline_total_filtrado'] = interacoes.count()
		context['timeline_filtros'] = timeline_filtros
		context['timeline_tipo_choices'] = InteracaoAcolhimento.TipoInteracao.choices
		context['interacao_form'] = kwargs.get('interacao_form', InteracaoAcolhimentoForm())
		context['tem_novo_retorno'] = self.object.mensagens.filter(
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			visualizada_equipe_em__isnull=True,
		).exists()
		# A ultima coisa que a pessoa disse foi o botao "Agora nao" do template da Meta.
		ultima_entrada = (
			self.object.mensagens.filter(direcao=MensagemContato.DirecaoChoices.ENTRADA)
			.order_by('-enfileirada_em', '-id')
			.first()
		)
		context['pediu_falar_depois'] = (
			ultima_entrada
			if ultima_entrada and (ultima_entrada.metadata_resposta or {}).get('meta_botao') == 'agora_nao'
			else None
		)

		# Fluxo evolutivo do acolhimento, na ordem de declaracao do TextChoices.
		valores = list(PrimeiroContato.StatusAcolhimento.values)
		atual_index = valores.index(self.object.status) if self.object.status in valores else -1
		status_fluxo = []
		for indice, (valor, rotulo) in enumerate(PrimeiroContato.StatusAcolhimento.choices):
			if indice < atual_index:
				estado = 'concluido'
			elif indice == atual_index:
				estado = 'atual'
			else:
				estado = 'futuro'
			status_fluxo.append({'valor': valor, 'rotulo': rotulo, 'estado': estado})

		context['status_choices'] = PrimeiroContato.StatusAcolhimento.choices
		context['status_atual'] = self.object.status
		context['status_fluxo'] = status_fluxo

		user = self.request.user
		ver_questionario = pode_ver_questionario(user)
		context['pode_ver_questionario'] = ver_questionario
		context['pode_excluir_convite'] = user.is_staff or user.is_superuser

		# Gabinete pastoral: nem os registros vao para o contexto de quem nao pode ver.
		ver_gabinete = pode_ver_gabinete(user)
		context['pode_ver_gabinete'] = ver_gabinete
		if ver_gabinete:
			context['registros_gabinete'] = self.object.registros_gabinete.select_related('criado_por')
			context['gabinete_form'] = RegistroGabineteForm()

		pode_gerenciar_responsavel = user.is_staff or user.is_superuser
		context['pode_gerenciar_responsavel'] = pode_gerenciar_responsavel
		if pode_gerenciar_responsavel:
			context['usuarios_responsaveis'] = (
				get_user_model().objects.filter(is_active=True).order_by('first_name', 'username')
			)
		else:
			context['usuarios_responsaveis'] = get_user_model().objects.none()

		convites = []
		if ver_questionario:
			convites = list(self.object.convites_questionario.select_related('questionario'))
			for convite in convites:
				convite.link = self.request.build_absolute_uri(
					reverse('responder-questionario', kwargs={'token': convite.token})
				)
			context['convites_questionario'] = convites
			questionarios_ativos = Questionario.objects.filter(ativo=True)
			membresia = Questionario.membresia()
			if membresia and self.object.status != PrimeiroContato.StatusAcolhimento.PARTICIPANTE:
				questionarios_ativos = questionarios_ativos.exclude(pk=membresia.pk)
			context['questionarios_ativos'] = questionarios_ativos
		else:
			context['convites_questionario'] = []
			context['questionarios_ativos'] = Questionario.objects.none()

		# `?membresia=<pk>` chega do redirect da troca de status: abre o pop-up
		# perguntando se a equipe quer preencher agora ou enviar o link.
		convite_destacado = (self.request.GET.get('membresia') or '').strip()
		context['membresia_convite'] = next(
			(
				convite for convite in convites
				if str(convite.pk) == convite_destacado and not convite.respondido
			),
			None,
		)

		# Aba aberta: vem do ?aba= (redirects e F5) e so vale se o usuario pode ver a aba.
		abas = {'timeline', 'dados'}
		if ver_questionario:
			abas.add('questionarios')
		if ver_gabinete:
			abas.add('gabinete')
		aba = (self.request.GET.get('aba') or '').strip()
		context['aba_ativa'] = aba if aba in abas else 'timeline'
		return context

	def post(self, request, *args, **kwargs):
		self.object = self.get_object()
		form = InteracaoAcolhimentoForm(request.POST)

		if form.is_valid():
			interacao = form.save(commit=False)
			interacao.pessoa = self.object
			interacao.save()
			messages.success(request, 'Evento adicionado na timeline com sucesso.')
			return redirect('pessoas-detalhe', pk=self.object.pk)

		messages.error(request, 'Nao foi possivel adicionar o evento. Revise os dados informados.')
		context = self.get_context_data(interacao_form=form)
		return self.render_to_response(context)


class RegistroGabineteCreateView(LoginRequiredMixin, GabinetePermissaoMixin, View):
	"""Registra um gabinete (agendado/realizado) na ficha. Fica fora da timeline geral."""

	def post(self, request, pk):
		pessoa = get_object_or_404(PrimeiroContato, pk=pk)
		form = RegistroGabineteForm(request.POST)
		if form.is_valid():
			registro = form.save(commit=False)
			registro.pessoa = pessoa
			registro.criado_por = request.user
			registro.save()
			messages.success(request, f'{registro.get_tipo_display()} registrado.')
		else:
			messages.error(request, 'Nao foi possivel registrar o gabinete. Revise os dados informados.')
		return redirect_ficha(pessoa.pk, 'gabinete')


class PrimeiroContatoStatusUpdateView(LoginRequiredMixin, View):
	def post(self, request, pk, *args, **kwargs):
		pessoa = get_object_or_404(PrimeiroContato, pk=pk)
		novo_status = (request.POST.get('status') or '').strip()

		if novo_status not in PrimeiroContato.StatusAcolhimento.values:
			messages.error(request, 'Status invalido.')
			return redirect('pessoas-detalhe', pk=pessoa.pk)

		if novo_status == pessoa.status:
			messages.info(request, 'A pessoa ja esta neste status.')
			return redirect('pessoas-detalhe', pk=pessoa.pk)

		status_anterior_label = pessoa.get_status_display()
		pessoa.status = novo_status
		pessoa.save(update_fields=['status', 'atualizado_em'])

		InteracaoAcolhimento.objects.create(
			pessoa=pessoa,
			tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
			descricao=f'Status alterado de "{status_anterior_label}" para "{pessoa.get_status_display()}".',
		)

		messages.success(request, f'Status atualizado para "{pessoa.get_status_display()}".')

		# Participante e o gatilho da membresia: ja deixa o link pronto e abre o
		# pop-up para a equipe escolher entre preencher na hora ou enviar.
		if novo_status == PrimeiroContato.StatusAcolhimento.PARTICIPANTE:
			convite, criado = convite_membresia(pessoa, request.user)
			if pode_ver_questionario(request.user):
				if convite is None:
					messages.warning(
						request,
						'Nenhum questionario de membresia ativo com perguntas: o link nao foi gerado.',
					)
				elif not convite.respondido:
					if criado:
						messages.success(request, 'Link do questionario de membresia gerado.')
					url = reverse('pessoas-detalhe', kwargs={'pk': pessoa.pk})
					return redirect(f'{url}?membresia={convite.pk}')

		return redirect('pessoas-detalhe', pk=pessoa.pk)


class PrimeiroContatoResponsavelUpdateView(LoginRequiredMixin, AdminPermissaoMixin, View):
	"""Associa ou troca o responsavel atual. Apenas staff/superuser podem alterar."""

	@staticmethod
	def _rotulo(usuario):
		if usuario is None:
			return 'Ninguem'
		return usuario.get_full_name() or usuario.get_username()

	def post(self, request, pk, *args, **kwargs):
		pessoa = get_object_or_404(PrimeiroContato, pk=pk)
		responsavel_id = (request.POST.get('responsavel_atual') or '').strip()

		if responsavel_id:
			novo_responsavel = get_user_model().objects.filter(pk=responsavel_id, is_active=True).first()
			if novo_responsavel is None:
				messages.error(request, 'Responsavel invalido.')
				return redirect_ficha(pessoa.pk, 'dados')
		else:
			novo_responsavel = None

		anterior = pessoa.responsavel_atual
		if novo_responsavel == anterior:
			messages.info(request, 'Nenhuma alteracao no responsavel.')
			return redirect_ficha(pessoa.pk, 'dados')

		pessoa.responsavel_atual = novo_responsavel
		pessoa.save(update_fields=['responsavel_atual', 'atualizado_em'])

		InteracaoAcolhimento.objects.create(
			pessoa=pessoa,
			tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
			descricao=f'Responsavel alterado de "{self._rotulo(anterior)}" para "{self._rotulo(novo_responsavel)}".',
		)

		messages.success(request, f'Responsavel atualizado para "{self._rotulo(novo_responsavel)}".')
		return redirect_ficha(pessoa.pk, 'dados')


class PrimeiroContatoMensagensView(LoginRequiredMixin, ConversasPessoasPermissaoMixin, DetailView):
	template_name = 'pessoa_mensagens.html'
	model = PrimeiroContato
	context_object_name = 'pessoa'
	page_size = 20

	def get_canal_atual(self):
		canal_atual = self.request.GET.get('canal', 'todos').strip()
		canais_validos = {choice[0] for choice in MensagemContato.CanalChoices.choices}
		if canal_atual not in canais_validos:
			canal_atual = 'todos'
		return canal_atual

	def get_mensagens_queryset(self, canal_atual):
		mensagens_queryset = self.object.mensagens.select_related('campanha', 'criado_por')
		if canal_atual != 'todos':
			mensagens_queryset = mensagens_queryset.filter(canal=canal_atual)
		return mensagens_queryset

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		agora = timezone.now()
		self.object.mensagens.filter(
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			visualizada_equipe_em__isnull=True,
		).update(visualizada_equipe_em=agora)

		canal_atual = self.get_canal_atual()
		mensagens_queryset = self.get_mensagens_queryset(canal_atual)

		total_mensagens = mensagens_queryset.count()
		mensagens_recentes = list(mensagens_queryset.order_by('-enfileirada_em', '-id')[:self.page_size])
		abas_canal = [
			{
				'valor': 'todos',
				'rotulo': 'Todos',
				'total': self.object.mensagens.count(),
			},
			{
				'valor': MensagemContato.CanalChoices.WHATSAPP,
				'rotulo': 'WhatsApp',
				'total': self.object.mensagens.filter(canal=MensagemContato.CanalChoices.WHATSAPP).count(),
			},
			# Aba de e-mail desativada por enquanto (reativar quando o canal de e-mail voltar):
			# {
			# 	'valor': MensagemContato.CanalChoices.EMAIL,
			# 	'rotulo': 'E-mail',
			# 	'total': self.object.mensagens.filter(canal=MensagemContato.CanalChoices.EMAIL).count(),
			# },
		]

		mensagens = list(reversed(mensagens_recentes))
		context['mensagens'] = mensagens
		context['abas_canal'] = abas_canal
		context['canal_atual'] = canal_atual
		context['has_more_messages'] = total_mensagens > len(mensagens_recentes)
		context['oldest_message_id'] = mensagens[0].id if mensagens else ''
		context['enfileirar_mensagem_form'] = kwargs.get('enfileirar_mensagem_form', EnfileirarMensagemForm())

		ultima_entrada = ultima_entrada_em(self.object)
		context['ultima_entrada'] = ultima_entrada
		context['janela_fecha_em'] = (
			ultima_entrada + timedelta(hours=JANELA_ATENDIMENTO_HORAS) if ultima_entrada else None
		)
		context['janela_aberta'] = janela_atendimento_aberta(self.object)
		context['precisa_template_continuar'] = precisa_template_continuar(self.object)
		context['usa_evolution'] = template_config.usando_evolution()
		context['usa_meta'] = template_config.usando_meta()
		context['template_continuar_configurado'] = template_config.continuar_configurado()
		context['pode_enviar_template_continuar'] = pode_enviar_template_continuar(self.object)
		context['template_continuar_liberado_em'] = proximo_template_continuar_em(self.object)
		return context


class PrimeiroContatoMensagensMaisView(LoginRequiredMixin, ConversasPessoasPermissaoMixin, View):
	page_size = 20

	def get(self, request, pk, *args, **kwargs):
		pessoa = get_object_or_404(PrimeiroContato, pk=pk)
		canal_atual = request.GET.get('canal', 'todos').strip()
		canais_validos = {choice[0] for choice in MensagemContato.CanalChoices.choices}
		if canal_atual not in canais_validos:
			canal_atual = 'todos'

		mensagens_queryset = pessoa.mensagens.select_related('campanha', 'criado_por')
		if canal_atual != 'todos':
			mensagens_queryset = mensagens_queryset.filter(canal=canal_atual)

		before_id = request.GET.get('before', '').strip()
		if before_id.isdigit():
			mensagem_cursor = mensagens_queryset.filter(id=int(before_id)).first()
			if mensagem_cursor:
				mensagens_queryset = mensagens_queryset.filter(
					Q(enfileirada_em__lt=mensagem_cursor.enfileirada_em)
					| Q(enfileirada_em=mensagem_cursor.enfileirada_em, id__lt=mensagem_cursor.id)
				)

		mensagens_recentes = list(mensagens_queryset.order_by('-enfileirada_em', '-id')[: self.page_size + 1])
		has_more = len(mensagens_recentes) > self.page_size
		mensagens = list(reversed(mensagens_recentes[: self.page_size]))
		html = ''.join(
			render_to_string(
				'partials/mensagem_conversa_item.html',
				{'mensagem': mensagem, 'pessoa': pessoa, 'canal_atual': canal_atual},
				request=request,
			)
			for mensagem in mensagens
		)

		return JsonResponse(
			{
				'html': html,
				'has_more': has_more,
				'next_before': mensagens[0].id if mensagens and has_more else '',
			}
		)


class AguardandoRespostaView(LoginRequiredMixin, ListView):
	"""Quem mandou mensagem no WhatsApp e ainda espera resposta da equipe.

	Aberta a todos os usuarios (a equipe acompanha pelo celular). O texto das
	mensagens so aparece para quem pode conversar com as pessoas, a mesma regra
	da tela de conversa.
	"""

	template_name = 'aguardando_resposta.html'
	context_object_name = 'pessoas'
	paginate_by = 20

	def get_aba(self):
		return 'respondidas' if self.request.GET.get('aba') == 'respondidas' else 'aguardando'

	def filtrar(self, queryset):
		busca = self.request.GET.get('q', '').strip()
		responsavel = self.request.GET.get('responsavel', '').strip()
		nova = self.request.GET.get('nova', '').strip()
		status = self.request.GET.get('status', '').strip()

		if busca:
			queryset = queryset.filter(Q(nome__icontains=busca) | Q(telefone_whatsapp__icontains=busca))

		if responsavel == 'meus':
			queryset = queryset.filter(responsavel_atual=self.request.user)
		elif responsavel == 'sem':
			queryset = queryset.filter(responsavel_atual__isnull=True)
		elif responsavel.isdigit():
			queryset = queryset.filter(responsavel_atual_id=int(responsavel))

		if nova == 'sim':
			queryset = queryset.filter(data_primeiro_contato__gte=corte_pessoa_nova())
		elif nova == 'nao':
			queryset = queryset.filter(data_primeiro_contato__lt=corte_pessoa_nova())

		if status in PrimeiroContato.StatusAcolhimento.values:
			queryset = queryset.filter(status=status)

		return queryset

	def get_queryset(self):
		queryset = self.filtrar(anotar_conversa(PrimeiroContato.objects.select_related('responsavel_atual')))
		if self.get_aba() == 'respondidas':
			return filtrar_respondidas(queryset).order_by('-ultima_resposta', '-pk')

		queryset = filtrar_aguardando(queryset)
		if self.request.GET.get('ordem') == 'espera':
			return queryset.order_by('aguardando_desde', 'pk')
		return queryset.order_by('-ultima_entrada', '-pk')

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		user = self.request.user
		aba = self.get_aba()
		corte_nova = corte_pessoa_nova()

		pessoas = context['pessoas']
		if aba == 'respondidas':
			detalhar_respondidas(pessoas)
		else:
			detalhar_aguardando(pessoas)
		for pessoa in pessoas:
			pessoa.eh_nova = pessoa.data_primeiro_contato >= corte_nova

		# Atalhos do topo: retrato de toda a fila, independente dos filtros.
		context['resumo'] = filtrar_aguardando(anotar_conversa(PrimeiroContato.objects.all())).aggregate(
			total=Count('pk'),
			sem_responsavel=Count('pk', filter=Q(responsavel_atual__isnull=True)),
			novas=Count('pk', filter=Q(data_primeiro_contato__gte=corte_nova)),
			meus=Count('pk', filter=Q(responsavel_atual=user)),
		)
		filtradas = self.filtrar(anotar_conversa(PrimeiroContato.objects.all()))
		context['total_aguardando'] = filtrar_aguardando(filtradas).count()
		context['total_respondidas'] = filtrar_respondidas(filtradas).count()

		params = self.request.GET.copy()
		params.pop('page', None)
		context['filtro_query'] = params.urlencode()
		params.pop('aba', None)
		context['abas_query'] = params.urlencode()

		context['aba'] = aba
		context['busca'] = self.request.GET.get('q', '').strip()
		context['responsavel_selecionado'] = self.request.GET.get('responsavel', '').strip()
		context['nova_atual'] = self.request.GET.get('nova', '').strip()
		context['status_atual'] = self.request.GET.get('status', '').strip()
		context['ordem_atual'] = self.request.GET.get('ordem', '').strip()
		context['filtros_ativos'] = sum(
			1 for chave in ('q', 'responsavel', 'nova', 'status') if self.request.GET.get(chave, '').strip()
		)
		context['responsaveis'] = (
			get_user_model().objects.filter(primeiros_contatos_responsaveis__isnull=False)
			.distinct()
			.order_by('first_name', 'username')
		)
		context['status_choices'] = PrimeiroContato.StatusAcolhimento.choices
		context['pode_conversar'] = pode_conversar_pessoas(user)
		context['dias_pessoa_nova'] = DIAS_PESSOA_NOVA
		context['dias_respondidas'] = DIAS_RESPONDIDAS
		return context


class PrimeiroContatoEnfileirarMensagemView(LoginRequiredMixin, ConversasPessoasPermissaoMixin, View):
	def post(self, request, pk, *args, **kwargs):
		pessoa = get_object_or_404(PrimeiroContato, pk=pk)
		form = EnfileirarMensagemForm(request.POST)

		if form.is_valid():
			mensagem = form.save(commit=False)
			mensagem.pessoa = pessoa
			mensagem.criado_por = request.user
			mensagem.prioridade = 5
			mensagem.agendada_para = None
			# Canal fixo em WhatsApp por enquanto (envio por e-mail desativado).
			mensagem.canal = MensagemContato.CanalChoices.WHATSAPP
			mensagem.direcao = MensagemContato.DirecaoChoices.SAIDA
			mensagem.status_fila = MensagemContato.StatusFilaChoices.PENDENTE

			if mensagem.canal == MensagemContato.CanalChoices.WHATSAPP:
				bloqueio = motivo_bloqueio_livre(pessoa)
				if bloqueio:
					messages.error(request, bloqueio[1])
					return redirect('pessoas-mensagens', pk=pessoa.pk)

			mensagem.save()
			messages.success(request, 'Mensagem enfileirada com sucesso.')
			return redirect('pessoas-mensagens', pk=pessoa.pk)

		messages.error(request, 'Nao foi possivel enfileirar a mensagem. Verifique os campos.')
		for campo, erros in form.errors.items():
			for erro in erros:
				messages.error(request, f'{campo}: {erro}')
		return redirect('pessoas-mensagens', pk=pessoa.pk)


class EnviarTemplateContinuarView(LoginRequiredMixin, ConversasPessoasPermissaoMixin, View):
	"""Enfileira o template de continuacao para reabrir a janela de 24h do WhatsApp."""

	def post(self, request, pk, *args, **kwargs):
		pessoa = get_object_or_404(PrimeiroContato, pk=pk)
		tipo_template = TemplateWhatsapp.Tipo.CONTINUAR
		usa_evolution = template_config.usando_evolution()
		usa_meta = template_config.usando_meta()
		template_sid = '' if usa_meta else template_config.continuar_sid()
		if not template_config.continuar_configurado():
			messages.error(request, template_config.mensagem_nao_configurado(tipo_template))
			return redirect('pessoas-mensagens', pk=pessoa.pk)

		bloqueio = motivo_bloqueio_template_continuar(pessoa)
		if bloqueio:
			messages.error(request, bloqueio[1])
			return redirect('pessoas-mensagens', pk=pessoa.pk)

		try:
			variaveis_base = json.loads(template_config.continuar_variables() or '{}')
			if not isinstance(variaveis_base, dict):
				variaveis_base = {}
		except Exception:
			variaveis_base = {}

		nome = (pessoa.nome or '').strip() or 'amigo(a)'
		content_variables = json.dumps({**variaveis_base, '1': nome}, ensure_ascii=False)
		metadata_envio = {'tipo_template': tipo_template}
		if template_sid:
			metadata_envio['twilio_template'] = {
				'content_sid': template_sid,
				'content_variables': content_variables,
			}
		if usa_meta:
			metadata_envio['meta_template'] = template_config.montar_meta_template(tipo_template, pessoa)
		if usa_evolution:
			texto_evolution = template_config.continuar_texto_evolution().replace('{nome}', nome)
			metadata_envio['evolution_texto'] = texto_evolution
			conteudo = texto_evolution
		else:
			conteudo = 'Template de continuacao enfileirado'

		MensagemContato.objects.create(
			pessoa=pessoa,
			criado_por=request.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			prioridade=5,
			conteudo=conteudo,
			metadata_envio=metadata_envio,
		)
		messages.success(
			request,
			'Template de continuacao enfileirado. Quando a pessoa responder, a janela de 24h reabre.',
		)
		return redirect('pessoas-mensagens', pk=pessoa.pk)


class ConfiguracaoTemplatesView(LoginRequiredMixin, SuperAdminPermissaoMixin, FormView):
	"""Tela (superusuario) para trocar templates Twilio e textos WhatsApp."""

	template_name = 'configuracao_templates.html'
	form_class = TemplatesWhatsappForm
	success_url = reverse_lazy('configuracao-templates')
	raise_exception = True

	def get_initial(self):
		return {
			'opt_in_sid': template_config.opt_in_sid(),
			'opt_in_variables': template_config.opt_in_variables(),
			'opt_in_texto_evolution': template_config.opt_in_texto_evolution(),
			'opt_in_meta_nome': template_config.opt_in_meta_nome(),
			'opt_in_meta_idioma': template_config.opt_in_meta_idioma(),
			'continuar_sid': template_config.continuar_sid(),
			'continuar_variables': template_config.continuar_variables(),
			'continuar_texto_evolution': template_config.continuar_texto_evolution(),
			'continuar_meta_nome': template_config.continuar_meta_nome(),
			'continuar_meta_idioma': template_config.continuar_meta_idioma(),
		}

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context['twilio_habilitado'] = settings.TWILIO_ENABLED
		context['usa_evolution'] = template_config.usando_evolution()
		context['usa_meta'] = template_config.usando_meta()
		context['whatsapp_provider'] = template_config.provider()
		context['env_opt_in_sid'] = (settings.TWILIO_TEMPLATE_OPT_IN_SID or '').strip()
		context['env_continuar_sid'] = (settings.TWILIO_TEMPLATE_CONTINUAR_SID or '').strip()
		context['env_opt_in_texto_evolution'] = (settings.EVOLUTION_TEXTO_OPTIN or '').strip()
		context['env_continuar_texto_evolution'] = (settings.EVOLUTION_TEXTO_CONTINUAR or '').strip()
		context['env_opt_in_meta_nome'] = (getattr(settings, 'META_TEMPLATE_OPT_IN_NOME', '') or '').strip()
		context['env_continuar_meta_nome'] = (getattr(settings, 'META_TEMPLATE_CONTINUAR_NOME', '') or '').strip()
		context['configs'] = {
			cfg.tipo: cfg
			for cfg in TemplateWhatsapp.objects.select_related('atualizado_por')
		}
		return context

	def form_valid(self, form):
		dados = form.cleaned_data
		valores = {
			TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO: {
				'content_sid': dados['opt_in_sid'],
				'content_variables': dados['opt_in_variables'],
				'texto_evolution': dados['opt_in_texto_evolution'],
				'meta_nome': dados['opt_in_meta_nome'],
				'meta_idioma': dados['opt_in_meta_idioma'],
			},
			TemplateWhatsapp.Tipo.CONTINUAR: {
				'content_sid': dados['continuar_sid'],
				'content_variables': dados['continuar_variables'],
				'texto_evolution': dados['continuar_texto_evolution'],
				'meta_nome': dados['continuar_meta_nome'],
				'meta_idioma': dados['continuar_meta_idioma'],
			},
		}
		for tipo, defaults in valores.items():
			TemplateWhatsapp.objects.update_or_create(
				tipo=tipo,
				defaults={
					**defaults,
					'atualizado_por': self.request.user,
				},
			)
		messages.success(self.request, 'Templates do WhatsApp atualizados com sucesso.')
		return super().form_valid(form)

	def form_invalid(self, form):
		messages.error(self.request, 'Nao foi possivel salvar. Verifique os campos destacados.')
		return super().form_invalid(form)


class MensagemContatoExcluirView(LoginRequiredMixin, ConversasPessoasPermissaoMixin, View):
	def post(self, request, pk, *args, **kwargs):
		mensagem = get_object_or_404(MensagemContato.objects.select_related('pessoa'), pk=pk)
		pessoa = mensagem.pessoa
		primeira_mensagem = (
			MensagemContato.objects.filter(pessoa=pessoa)
			.order_by('enfileirada_em', 'id')
			.first()
		)
		eh_primeira_mensagem = bool(primeira_mensagem and primeira_mensagem.pk == mensagem.pk)
		next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('mensagens-fila')
		if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
			next_url = reverse('mensagens-fila')

		if mensagem.status_fila != MensagemContato.StatusFilaChoices.PENDENTE:
			messages.error(request, 'A mensagem so pode ser excluida enquanto estiver pendente.')
			return redirect(next_url)

		mensagem.delete()
		if eh_primeira_mensagem and pessoa.status == PrimeiroContato.StatusAcolhimento.ROBO:
			pessoa.status = PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO
			pessoa.save(update_fields=['status', 'atualizado_em'])
			messages.success(request, 'Mensagem excluida e status da pessoa retornado para Primeiro contato.')
			return redirect(next_url)

		messages.success(request, 'Mensagem excluida com sucesso.')
		return redirect(next_url)


@method_decorator(csrf_exempt, name='dispatch')
class TwilioStatusWebhookView(View):
	def post(self, request, *args, **kwargs):
		message_sid = (request.POST.get('MessageSid') or '').strip()
		message_status = (request.POST.get('MessageStatus') or '').strip().lower()
		error_code = (request.POST.get('ErrorCode') or '').strip()
		error_message = (request.POST.get('ErrorMessage') or '').strip()

		if not message_sid:
			return JsonResponse({'detail': 'MessageSid ausente.'}, status=400)

		mensagem = MensagemContato.objects.filter(referencia_externa=message_sid).first()
		if not mensagem:
			return JsonResponse({'detail': 'Mensagem nao encontrada.'}, status=404)

		callback_payload = {}
		for key in request.POST:
			values = request.POST.getlist(key)
			callback_payload[key] = values[0] if len(values) == 1 else values

		metadata = dict(mensagem.metadata_envio or {})
		webhook_data = metadata.get('twilio_webhook', [])
		if not isinstance(webhook_data, list):
			webhook_data = [webhook_data]
		webhook_data.append({'received_at': timezone.now().isoformat(), 'payload': callback_payload})
		metadata['twilio_webhook'] = webhook_data
		mensagem.metadata_envio = metadata

		status_processando = {'queued', 'accepted', 'sending'}
		status_enviada = {'sent'}
		status_entregue = {'delivered'}
		status_lida = {'read'}
		status_falha = {'undelivered', 'failed', 'canceled'}

		update_fields = ['metadata_envio', 'atualizado_em']

		if message_status in status_processando:
			mensagem.status_fila = MensagemContato.StatusFilaChoices.PROCESSANDO
			update_fields.extend(['status_fila'])
		elif message_status in status_enviada:
			mensagem.status_fila = MensagemContato.StatusFilaChoices.ENVIADA
			if not mensagem.enviada_em:
				mensagem.enviada_em = timezone.now()
			update_fields.extend(['status_fila', 'enviada_em'])
		elif message_status in status_entregue:
			mensagem.status_fila = MensagemContato.StatusFilaChoices.ENVIADA
			if not mensagem.enviada_em:
				mensagem.enviada_em = timezone.now()
			if not mensagem.entregue_em:
				mensagem.entregue_em = timezone.now()
			update_fields.extend(['status_fila', 'enviada_em', 'entregue_em'])
		elif message_status in status_lida:
			mensagem.status_fila = MensagemContato.StatusFilaChoices.ENVIADA
			if not mensagem.enviada_em:
				mensagem.enviada_em = timezone.now()
			if not mensagem.entregue_em:
				mensagem.entregue_em = timezone.now()
			mensagem.lida_em = timezone.now()
			update_fields.extend(['status_fila', 'enviada_em', 'entregue_em', 'lida_em'])
		elif message_status in status_falha:
			mensagem.status_fila = MensagemContato.StatusFilaChoices.FALHA
			mensagem.erro_ultimo_envio = ' | '.join([parte for parte in [error_code, error_message, message_status] if parte])
			update_fields.extend(['status_fila', 'erro_ultimo_envio'])

		mensagem.save(update_fields=list(dict.fromkeys(update_fields)))
		return JsonResponse({'detail': 'Webhook processado.'}, status=200)


@method_decorator(csrf_exempt, name='dispatch')
class TwilioInboundWebhookView(View):
	def post(self, request, *args, **kwargs):
		from_number = (request.POST.get('From') or '').strip()
		message_sid = (request.POST.get('MessageSid') or '').strip()
		body = (request.POST.get('Body') or '').strip()

		payload = {}
		for key in request.POST:
			values = request.POST.getlist(key)
			payload[key] = values[0] if len(values) == 1 else values

		if not from_number:
			return JsonResponse({'detail': 'From ausente no webhook.'}, status=400)

		pessoa = find_pessoa_by_phone(from_number)
		agora = timezone.now()
		if not pessoa:
			pessoa = PrimeiroContato.objects.create(
				nome=build_auto_nome_from_phone(from_number),
				telefone_whatsapp=phone_for_cadastro(from_number),
				primeira_vez=True,
				como_conheceu=PrimeiroContato.ComoConheceuChoices.OUTRO,
				o_que_busca=PrimeiroContato.OQueBuscaChoices.PARTICIPAR_DE_ALGO,
				origem_cadastro=PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO,
				iniciou_interacao=True,
				status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
				observacoes='Pre-cadastro criado automaticamente via webhook do WhatsApp.',
			)
			InteracaoAcolhimento.objects.create(
				pessoa=pessoa,
				tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
				descricao='Pessoa iniciou a interacao enviando mensagem no WhatsApp.',
				data_interacao=agora.date(),
			)
		elif not pessoa.iniciou_interacao:
			pessoa.iniciou_interacao = True
			update_fields = ['iniciou_interacao', 'atualizado_em']
			if pessoa.status in {
				PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
				PrimeiroContato.StatusAcolhimento.ROBO,
			}:
				pessoa.status = PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
				update_fields.append('status')
			pessoa.save(update_fields=update_fields)
			InteracaoAcolhimento.objects.create(
				pessoa=pessoa,
				tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
				descricao='Pessoa respondeu no WhatsApp e liberou o envio de mensagens.',
				data_interacao=agora.date(),
			)

		conteudo = body or '(mensagem sem texto)'

		MensagemContato.objects.create(
			pessoa=pessoa,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo=conteudo,
			referencia_externa=message_sid,
			enviada_em=agora,
			entregue_em=agora,
			visualizada_equipe_em=None,
			metadata_resposta={'twilio': payload},
		)

		ultima_saida = (
			MensagemContato.objects.filter(
				pessoa=pessoa,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
			)
			.order_by('-enviada_em', '-enfileirada_em', '-id')
			.first()
		)

		if ultima_saida:
			ultima_saida.resposta_recebida_em = agora
			ultima_saida.resposta_conteudo = conteudo
			metadata_resposta = dict(ultima_saida.metadata_resposta or {})
			webhook_data = metadata_resposta.get('twilio_webhook', [])
			if not isinstance(webhook_data, list):
				webhook_data = [webhook_data]
			webhook_data.append({'received_at': agora.isoformat(), 'payload': payload})
			metadata_resposta['twilio_webhook'] = webhook_data
			ultima_saida.metadata_resposta = metadata_resposta
			ultima_saida.save(
				update_fields=[
					'resposta_recebida_em',
					'resposta_conteudo',
					'metadata_resposta',
					'atualizado_em',
				]
			)

		return JsonResponse({'detail': 'Mensagem de entrada processada.'}, status=200)


class PrimeiroContatoUpdateView(LoginRequiredMixin, UpdateView):
	template_name = 'pessoa_form.html'
	model = PrimeiroContato
	form_class = PrimeiroContatoForm
	success_url = reverse_lazy('pessoas-lista')

	def form_valid(self, form):
		response = super().form_valid(form)
		messages.success(self.request, 'Cadastro atualizado com sucesso.')
		return response

	def form_invalid(self, form):
		messages.error(self.request, 'Nao foi possivel atualizar. Verifique os campos e tente novamente.')
		return super().form_invalid(form)


class PrimeiroContatoDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
	template_name = 'pessoa_confirm_delete.html'
	model = PrimeiroContato
	context_object_name = 'pessoa'
	success_url = reverse_lazy('pessoas-lista')
	raise_exception = True

	def test_func(self):
		return self.request.user.is_staff or self.request.user.is_superuser

	def delete(self, request, *args, **kwargs):
		messages.success(request, 'Cadastro excluido com sucesso.')
		return super().delete(request, *args, **kwargs)


class PrimeiroContatoExportCsvView(LoginRequiredMixin, PrimeiroContatoQuerysetMixin, View):
	def get(self, request, *args, **kwargs):
		queryset = self.get_filtered_queryset(PrimeiroContato.objects.all())

		response = HttpResponse(content_type='text/csv; charset=utf-8')
		response['Content-Disposition'] = 'attachment; filename="primeiros_contatos.csv"'

		writer = csv.writer(response)
		writer.writerow([
			'Nome',
			'Telefone WhatsApp',
			'Primeira vez',
			'Como conheceu',
			'O que busca',
			'Responsavel atual',
			'E-mail',
			'Genero',
			'Idade',
			'Religiao',
			'Estado civil',
			'Cidade',
			'Status',
			'Data do primeiro contato',
			'Observacoes',
		])

		for pessoa in queryset:
			if pessoa.responsavel_atual:
				responsavel_nome = pessoa.responsavel_atual.get_full_name() or pessoa.responsavel_atual.get_username()
			else:
				responsavel_nome = ''

			writer.writerow([
				pessoa.nome,
				pessoa.telefone_whatsapp,
				'Sim' if pessoa.primeira_vez else 'Nao',
				pessoa.get_como_conheceu_display(),
				pessoa.get_o_que_busca_display(),
				responsavel_nome,
				pessoa.email,
				pessoa.get_genero_display(),
				pessoa.idade,
				pessoa.religiao,
				pessoa.get_estado_civil_display(),
				pessoa.cidade,
				pessoa.get_status_display(),
				pessoa.data_primeiro_contato.strftime('%d/%m/%Y'),
				pessoa.observacoes,
			])

		return response


class RelatorioPessoasView(AdminPermissaoMixin, View):
	template_name = 'relatorio_pessoas.html'

	def get(self, request, *args, **kwargs):
		tem_filtros = bool(request.GET)
		form = RelatorioPessoasForm(request.GET or None)
		context = {'form': form, 'resumo': None}

		if tem_filtros:
			if form.is_valid():
				queryset = filtrar_pessoas(form.cleaned_data)
				if request.GET.get('acao') == 'gerar':
					return gerar_relatorio(
						queryset,
						form.cleaned_data['colunas'],
						form.cleaned_data['formato'],
						resumo_filtros(form.cleaned_data),
					)
				context['resumo'] = resumo_pessoas(queryset)
		else:
			context['resumo'] = resumo_pessoas(PrimeiroContato.objects.all())

		return render(request, self.template_name, context)


# ----------------------------------------------------------------------------
# Questionarios (construtor + convites + resposta publica)
# ----------------------------------------------------------------------------

class QuestionarioListView(SuperAdminPermissaoMixin, ListView):
	template_name = 'questionarios_lista.html'
	model = Questionario
	context_object_name = 'questionarios'

	def get_queryset(self):
		return Questionario.objects.annotate(total_perguntas=Count('perguntas'))


class ConviteRespostasQuerysetMixin:
	"""Filtros da central de respostas: a listagem e a exportacao usam os mesmos."""

	def get_filtered_queryset(self):
		queryset = (
			ConviteQuestionario.objects
			.select_related('pessoa', 'questionario', 'criado_por', 'respondido_por')
			.prefetch_related(
				'questionario__perguntas',
				Prefetch(
					'respostas',
					queryset=RespostaPergunta.objects.select_related('pergunta', 'opcao').order_by(
						'pergunta__ordem', 'pergunta__id'
					),
				),
			)
		)

		busca = self.request.GET.get('q', '').strip()
		status = self.request.GET.get('status', '').strip()
		questionario_id = self.request.GET.get('questionario', '').strip()
		ordem = self.request.GET.get('ordem', 'recentes').strip()

		if busca:
			queryset = queryset.filter(
				Q(pessoa__nome__icontains=busca)
				| Q(pessoa__telefone_whatsapp__icontains=busca)
				| Q(respondente_nome__icontains=busca)
				| Q(questionario__titulo__icontains=busca)
				| Q(respostas__valor_texto__icontains=busca)
				| Q(respostas__opcao__texto__icontains=busca)
			).distinct()

		if status in ConviteQuestionario.StatusConvite.values:
			queryset = queryset.filter(status=status)

		if questionario_id.isdigit():
			queryset = queryset.filter(questionario_id=int(questionario_id))

		if ordem == 'antigos':
			queryset = queryset.order_by('criado_em', 'id')
		elif ordem == 'respondidos':
			queryset = queryset.order_by(F('respondido_em').desc(nulls_last=True), '-criado_em', '-id')
		else:
			queryset = queryset.order_by('-criado_em', '-id')

		return queryset

	def link_convite(self, convite):
		return self.request.build_absolute_uri(
			reverse('responder-questionario', kwargs={'token': convite.token})
		)


class QuestionarioRespostasListView(ConversasPessoasPermissaoMixin, ConviteRespostasQuerysetMixin, ListView):
	template_name = 'questionario_respostas.html'
	model = ConviteQuestionario
	context_object_name = 'convites'
	paginate_by = 12

	def get_queryset(self):
		return self.get_filtered_queryset()

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		query_params = self.request.GET.copy()
		query_params.pop('page', None)

		for convite in context['convites']:
			respostas = {resposta.pergunta_id: resposta for resposta in convite.respostas.all()}
			convite.itens_resposta = [
				{'pergunta': pergunta, 'resposta': respostas.get(pergunta.id)}
				for pergunta in convite.questionario.perguntas.all()
			]
			convite.link = self.link_convite(convite)

		resumo = ConviteQuestionario.objects.aggregate(
			total=Count('id'),
			pendentes=Count(
				'id',
				filter=Q(status=ConviteQuestionario.StatusConvite.PENDENTE),
			),
			respondidos=Count(
				'id',
				filter=Q(status=ConviteQuestionario.StatusConvite.RESPONDIDO),
			),
		)

		context['busca'] = self.request.GET.get('q', '').strip()
		context['status_atual'] = self.request.GET.get('status', '').strip()
		context['questionario_atual'] = self.request.GET.get('questionario', '').strip()
		context['ordem_atual'] = self.request.GET.get('ordem', 'recentes').strip()
		context['status_choices'] = ConviteQuestionario.StatusConvite.choices
		context['questionarios'] = Questionario.objects.order_by('titulo')
		context['resumo_convites'] = resumo
		context['total_filtrado'] = context['paginator'].count
		context['filtro_query'] = query_params.urlencode()
		return context


class QuestionarioRespostasExportView(ConversasPessoasPermissaoMixin, ConviteRespostasQuerysetMixin, View):
	"""Exporta (Excel ou CSV) todos os convites do filtro aplicado, sem paginacao."""

	def get(self, request, *args, **kwargs):
		return gerar_exportacao_respostas(
			list(self.get_filtered_queryset()),
			request.GET.get('formato', 'xlsx'),
			self.link_convite,
		)


class QuestionarioCreateView(SuperAdminPermissaoMixin, CreateView):
	template_name = 'questionario_form.html'
	form_class = QuestionarioForm

	def form_valid(self, form):
		form.instance.criado_por = self.request.user
		self.object = form.save()
		messages.success(self.request, 'Questionario criado. Adicione as perguntas.')
		return redirect('questionario-builder', pk=self.object.pk)


class QuestionarioUpdateView(SuperAdminPermissaoMixin, UpdateView):
	template_name = 'questionario_form.html'
	model = Questionario
	form_class = QuestionarioForm

	def get_success_url(self):
		return reverse('questionario-builder', kwargs={'pk': self.object.pk})


class QuestionarioDeleteView(SuperAdminPermissaoMixin, DeleteView):
	template_name = 'questionario_confirm_delete.html'
	model = Questionario
	context_object_name = 'questionario'
	success_url = reverse_lazy('questionarios-lista')


class QuestionarioBuilderView(SuperAdminPermissaoMixin, View):
	template_name = 'questionario_builder.html'

	def _render(self, request, questionario, form):
		return render(request, self.template_name, {
			'questionario': questionario,
			'perguntas': questionario.perguntas.prefetch_related('opcoes'),
			'form': form,
		})

	def get(self, request, pk):
		questionario = get_object_or_404(Questionario, pk=pk)
		return self._render(request, questionario, PerguntaForm())

	def post(self, request, pk):
		questionario = get_object_or_404(Questionario, pk=pk)
		form = PerguntaForm(request.POST)
		if form.is_valid():
			pergunta = form.save(commit=False)
			pergunta.questionario = questionario
			pergunta.ordem = (questionario.perguntas.aggregate(m=Max('ordem'))['m'] or 0) + 1
			pergunta.save()
			form.salvar_opcoes(pergunta)
			messages.success(request, 'Pergunta adicionada.')
			return redirect('questionario-builder', pk=questionario.pk)
		messages.error(request, 'Nao foi possivel adicionar a pergunta. Verifique os campos.')
		return self._render(request, questionario, form)


class QuestionarioPreviewView(SuperAdminPermissaoMixin, DetailView):
	template_name = 'questionario_preview.html'
	model = Questionario
	context_object_name = 'questionario'

	def get_queryset(self):
		return Questionario.objects.prefetch_related('perguntas__opcoes')

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		form = ResponderQuestionarioForm(
			questionario=self.object,
			initial={'respondente_nome': 'Nome do respondente'},
		)
		for field in form.fields.values():
			field.disabled = True
		context['form'] = form
		return context


class PerguntaUpdateView(SuperAdminPermissaoMixin, UpdateView):
	template_name = 'pergunta_form.html'
	model = PerguntaQuestionario
	form_class = PerguntaForm
	context_object_name = 'pergunta'

	def get_success_url(self):
		messages.success(self.request, 'Pergunta atualizada.')
		return reverse('questionario-builder', kwargs={'pk': self.object.questionario_id})


class PerguntaDeleteView(SuperAdminPermissaoMixin, View):
	def post(self, request, pk):
		pergunta = get_object_or_404(PerguntaQuestionario, pk=pk)
		questionario_id = pergunta.questionario_id
		pergunta.delete()
		messages.success(request, 'Pergunta removida.')
		return redirect('questionario-builder', pk=questionario_id)


class PerguntaMoverView(SuperAdminPermissaoMixin, View):
	def post(self, request, pk):
		pergunta = get_object_or_404(PerguntaQuestionario, pk=pk)
		direcao = request.POST.get('direcao')
		perguntas = list(pergunta.questionario.perguntas.all())
		ids = [p.id for p in perguntas]
		indice = ids.index(pergunta.id)
		alvo = indice - 1 if direcao == 'subir' else indice + 1
		if 0 <= alvo < len(perguntas):
			perguntas[indice], perguntas[alvo] = perguntas[alvo], perguntas[indice]
			for nova_ordem, item in enumerate(perguntas):
				if item.ordem != nova_ordem:
					item.ordem = nova_ordem
					item.save(update_fields=['ordem'])
		return redirect('questionario-builder', pk=pergunta.questionario_id)


class GerarConviteQuestionarioView(ConversasPessoasPermissaoMixin, View):
	def post(self, request, pk):
		pessoa = get_object_or_404(PrimeiroContato, pk=pk)
		questionario = get_object_or_404(Questionario, pk=request.POST.get('questionario'), ativo=True)
		bloqueio = motivo_bloqueio_convite(pessoa, questionario)
		if bloqueio:
			messages.error(request, bloqueio)
			return redirect_ficha(pessoa.pk, 'questionarios')

		if not questionario.perguntas.exists():
			messages.error(request, 'Este questionario ainda nao tem perguntas.')
			return redirect_ficha(pessoa.pk, 'questionarios')

		# Evita duplicar link ao clicar mais de uma vez: reaproveita o pendente existente.
		ja_pendente = ConviteQuestionario.objects.filter(
			pessoa=pessoa,
			questionario=questionario,
			status=ConviteQuestionario.StatusConvite.PENDENTE,
		).exists()
		if ja_pendente:
			messages.info(request, 'Ja existe um link pendente para este questionario.')
			return redirect_ficha(pessoa.pk, 'questionarios')

		with transaction.atomic():
			ConviteQuestionario.objects.create(
				questionario=questionario,
				pessoa=pessoa,
				criado_por=request.user,
			)
			InteracaoAcolhimento.objects.create(
				pessoa=pessoa,
				tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
				descricao=f'Link do questionario "{questionario.titulo}" gerado para preenchimento.',
			)
		messages.success(request, 'Link do questionario gerado.')
		return redirect_ficha(pessoa.pk, 'questionarios')


class ExcluirConviteQuestionarioView(AdminPermissaoMixin, View):
	def post(self, request, pk):
		convite = get_object_or_404(
			ConviteQuestionario.objects.select_related('pessoa', 'questionario'), pk=pk
		)
		pessoa = convite.pessoa
		pessoa_id = convite.pessoa_id
		questionario_titulo = convite.questionario.titulo
		with transaction.atomic():
			InteracaoAcolhimento.objects.create(
				pessoa=pessoa,
				tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
				descricao=f'Link do questionario "{questionario_titulo}" apagado.',
			)
			convite.delete()
		messages.success(request, 'Link do questionario removido.')
		return redirect_ficha(pessoa_id, 'questionarios')


class EnviarConviteWhatsappView(ConversasPessoasPermissaoMixin, View):
	def post(self, request, pk):
		convite = get_object_or_404(
			ConviteQuestionario.objects.select_related('pessoa', 'questionario'), pk=pk
		)
		pessoa = convite.pessoa
		bloqueio = motivo_bloqueio_livre(pessoa)
		if bloqueio:
			messages.error(request, bloqueio[1])
			return redirect_ficha(pessoa.pk, 'questionarios')

		link = request.build_absolute_uri(
			reverse('responder-questionario', kwargs={'token': convite.token})
		)
		conteudo = (
			f'Ola, {pessoa.nome}! Segue o link para responder o questionario '
			f'"{convite.questionario.titulo}": {link}'
		)
		with transaction.atomic():
			MensagemContato.objects.create(
				pessoa=pessoa,
				criado_por=request.user,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
				status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
				prioridade=5,
				conteudo=conteudo,
			)
			InteracaoAcolhimento.objects.create(
				pessoa=pessoa,
				tipo=InteracaoAcolhimento.TipoInteracao.TENTATIVA_CONTATO,
				descricao=(
					f'Mensagem com link do questionario "{convite.questionario.titulo}" '
					'enfileirada para envio pelo WhatsApp.'
				),
			)
		messages.success(request, 'Mensagem com o link enfileirada no WhatsApp.')
		return redirect_ficha(pessoa.pk, 'questionarios')


class RespostaConviteDetailView(ConversasPessoasPermissaoMixin, DetailView):
	template_name = 'resposta_detail.html'
	model = ConviteQuestionario
	context_object_name = 'convite'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		respostas = {r.pergunta_id: r for r in self.object.respostas.select_related('opcao', 'pergunta')}
		context['itens'] = [
			{'pergunta': pergunta, 'resposta': respostas.get(pergunta.id)}
			for pergunta in self.object.questionario.perguntas.all()
		]
		return context


class ResponderQuestionarioView(View):
	"""Pagina publica (sem login) para responder um questionario via token."""

	template_name = 'responder_questionario.html'

	def get_convite(self, token):
		return get_object_or_404(
			ConviteQuestionario.objects.select_related('pessoa', 'questionario'), token=token
		)

	def get(self, request, token):
		convite = self.get_convite(token)
		if convite.respondido:
			return render(request, self.template_name, {'convite': convite, 'ja_respondido': True})
		nome_inicial = request.user.get_full_name() if request.user.is_authenticated else ''
		form = ResponderQuestionarioForm(
			questionario=convite.questionario,
			initial={'respondente_nome': nome_inicial},
		)
		return render(request, self.template_name, {'convite': convite, 'form': form})

	def post(self, request, token):
		convite = self.get_convite(token)
		if convite.respondido:
			return render(request, self.template_name, {'convite': convite, 'ja_respondido': True})

		form = ResponderQuestionarioForm(request.POST, questionario=convite.questionario)
		if form.is_valid():
			with transaction.atomic():
				form.salvar_respostas(convite)
				convite.status = ConviteQuestionario.StatusConvite.RESPONDIDO
				convite.respondido_em = timezone.now()
				convite.respondente_nome = form.cleaned_data['respondente_nome']
				if request.user.is_authenticated:
					convite.respondido_por = request.user
				convite.save(update_fields=['status', 'respondido_em', 'respondente_nome', 'respondido_por'])
				InteracaoAcolhimento.objects.create(
					pessoa=convite.pessoa,
					tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
					descricao=(
						f'Questionario "{convite.questionario.titulo}" respondido por '
						f'{convite.respondente_nome}.'
					),
				)
			return redirect('responder-questionario-sucesso')

		messages.error(request, 'Verifique os campos obrigatorios e tente novamente.')
		return render(request, self.template_name, {'convite': convite, 'form': form})


class ResponderSucessoView(TemplateView):
	template_name = 'responder_sucesso.html'
