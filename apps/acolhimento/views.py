import csv
import threading

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.http import HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db import close_old_connections
from django.db.models import Count, Exists, OuterRef, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, FormView, ListView, TemplateView, UpdateView

from apps.acolhimento.fila_processor import processar_fila_mensagens
from apps.acolhimento.forms import AutoCadastroPrimeiroContatoForm, DisparoMensagemMassaForm, EnfileirarMensagemForm, InteracaoAcolhimentoForm, PrimeiroContatoForm, RelatorioPessoasForm
from apps.acolhimento.models import ExecucaoProcessamentoFila, InteracaoAcolhimento, MensagemContato, PrimeiroContato
from apps.acolhimento.phone_utils import build_auto_nome_from_phone, find_pessoa_by_phone, phone_for_cadastro
from apps.acolhimento.reports import filtrar_pessoas, gerar_relatorio, resumo_filtros, resumo_pessoas
from apps.acolhimento.whatsapp_rules import WHATSAPP_OPTIN_REQUIRED_ERROR, pessoa_pode_receber_whatsapp


PERMISSAO_CONVERSAR_PESSOAS = 'acolhimento.pode_conversar_pessoas'


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


class ConversasPessoasPermissaoMixin(UserPassesTestMixin):
	raise_exception = True

	def test_func(self):
		user = self.request.user
		return user.is_staff or user.is_superuser or user.has_perm(PERMISSAO_CONVERSAR_PESSOAS)


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
		origem = self.request.GET.get('origem', '').strip()
		iniciou_interacao = self.request.GET.get('iniciou_interacao', '').strip()
		sort_coluna, direcao = self.get_sort_state()

		if busca:
			queryset = queryset.filter(
				Q(nome__icontains=busca)
				| Q(telefone_whatsapp__icontains=busca)
				| Q(email__icontains=busca)
				| Q(religiao__icontains=busca)
			)

		if origem:
			queryset = queryset.filter(origem_cadastro=origem)

		if iniciou_interacao == 'sim':
			queryset = queryset.filter(iniciou_interacao=True)
		elif iniciou_interacao == 'nao':
			queryset = queryset.filter(iniciou_interacao=False)

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
		context['origem_atual'] = self.request.GET.get('origem', '').strip()
		context['iniciou_interacao_atual'] = self.request.GET.get('iniciou_interacao', '').strip()
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

	def get_filtered_queryset(self, queryset):
		busca = self.request.GET.get('q', '').strip()
		status = self.request.GET.get('status', '').strip()
		canal = self.request.GET.get('canal', '').strip()
		direcao_mensagem = self.request.GET.get('direcao_mensagem', '').strip()
		enviado = self.request.GET.get('enviado', '').strip()
		resposta_status = self.request.GET.get('resposta_status', '').strip()
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
		fila_queryset = self.get_queryset()
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
		]
		context['sort_coluna_atual'] = sort_coluna_atual
		context['sort_direcao_atual'] = sort_direcao_atual
		context['sort_links'] = sort_links
		context['total_filtrado'] = context['paginator'].count
		context['filtro_query'] = filtro_query
		context['fila_resumo_cards'] = [
			{'rotulo': 'Pendentes', 'valor': resumo_fila['pendentes'], 'classe': 'is-pending'},
			{'rotulo': 'Falhas', 'valor': resumo_fila['falhas'], 'classe': 'is-failed'},
			{'rotulo': 'Canceladas', 'valor': resumo_fila['canceladas'], 'classe': 'is-canceled'},
			{'rotulo': 'Sem resposta', 'valor': resumo_fila['sem_resposta'], 'classe': 'is-waiting'},
			{'rotulo': 'Recebidas', 'valor': resumo_fila['recebidas'], 'classe': 'is-inbound'},
		]
		return context


def _append_execucao_log(execucao: ExecucaoProcessamentoFila, texto: str):
	linha = f'[{timezone.now().strftime("%d/%m/%Y %H:%M:%S")}] {texto}'
	execucao.log_execucao = f'{execucao.log_execucao}\n{linha}'.strip()
	execucao.save(update_fields=['log_execucao', 'atualizado_em'])


def _run_execucao_fila(execucao_id: int):
	close_old_connections()
	execucao = ExecucaoProcessamentoFila.objects.get(pk=execucao_id)

	def _progress(texto: str):
		nonlocal execucao
		execucao.refresh_from_db(fields=['id', 'log_execucao', 'atualizado_em'])
		_append_execucao_log(execucao, texto)

	def _should_stop() -> bool:
		execucao.refresh_from_db(fields=['solicitar_parada'])
		return bool(execucao.solicitar_parada)

	try:
		resultado = processar_fila_mensagens(
			limit=execucao.limite,
			ids=[int(i) for i in execucao.ids_filtrados if str(i).isdigit()],
			dry_run=execucao.dry_run,
			progress_callback=_progress,
			should_stop=_should_stop,
		)

		execucao.refresh_from_db()
		execucao.total_selecionado = resultado['total_selecionado']
		execucao.total_processado = resultado['total_processado']
		execucao.total_sucesso = resultado['sucesso']
		execucao.total_falha = resultado['falha']
		execucao.finalizado_em = timezone.now()
		execucao.status = (
			ExecucaoProcessamentoFila.StatusExecucaoChoices.INTERROMPIDA
			if resultado['interrompido']
			else ExecucaoProcessamentoFila.StatusExecucaoChoices.CONCLUIDA
		)
		execucao.save(
			update_fields=[
				'total_selecionado',
				'total_processado',
				'total_sucesso',
				'total_falha',
				'finalizado_em',
				'status',
				'atualizado_em',
			]
		)
	except Exception as exc:  # pragma: no cover
		execucao.refresh_from_db()
		_append_execucao_log(execucao, f'Erro inesperado: {exc}')
		execucao.status = ExecucaoProcessamentoFila.StatusExecucaoChoices.FALHA
		execucao.finalizado_em = timezone.now()
		execucao.save(update_fields=['status', 'finalizado_em', 'atualizado_em'])
	finally:
		close_old_connections()


class ProcessamentoFilaControleView(LoginRequiredMixin, MensagensPermissaoMixin, TemplateView):
	template_name = 'mensagens_processamento.html'

	def post(self, request, *args, **kwargs):
		action = (request.POST.get('action') or '').strip()

		if action == 'iniciar':
			em_execucao = ExecucaoProcessamentoFila.objects.filter(
				status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO
			).exists()
			if em_execucao:
				messages.error(request, 'Ja existe um processamento em execucao.')
				return redirect('mensagens-processamento')

			try:
				limite = max(int(request.POST.get('limit', '20')), 1)
			except ValueError:
				limite = 20

			dry_run = request.POST.get('dry_run') == 'on'
			execucao = ExecucaoProcessamentoFila.objects.create(
				solicitado_por=request.user,
				status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO,
				limite=limite,
				dry_run=dry_run,
			)
			threading.Thread(target=_run_execucao_fila, args=(execucao.id,), daemon=True).start()
			messages.success(request, f'Processamento #{execucao.id} iniciado.')
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

		return queryset.order_by('nome').distinct()

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
		context['total_pessoas_filtradas'] = queryset_filtrado.count()
		context['selected_pessoas'] = selected_pessoas
		return context

	def form_valid(self, form):
		pessoas = list(form.cleaned_data['pessoas'])
		canal = form.cleaned_data['canal']
		conteudo = form.cleaned_data['conteudo']

		if canal == MensagemContato.CanalChoices.WHATSAPP:
			pessoas_bloqueadas = [pessoa for pessoa in pessoas if not pessoa_pode_receber_whatsapp(pessoa)]
			pessoas = [pessoa for pessoa in pessoas if pessoa_pode_receber_whatsapp(pessoa)]

			if pessoas_bloqueadas:
				messages.warning(
					self.request,
					f'{len(pessoas_bloqueadas)} pessoa(s) ignorada(s): ainda nao responderam ao template de primeiro contato.',
				)

			if not pessoas:
				messages.error(self.request, 'Nenhuma mensagem WhatsApp foi criada. Todos os selecionados ainda aguardam resposta ao template.')
				return redirect(self.success_url)

		mensagens = [
			MensagemContato(
				pessoa=pessoa,
				criado_por=self.request.user,
				canal=canal,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
				status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
				prioridade=5,
				agendada_para=None,
				conteudo=conteudo,
			)
			for pessoa in pessoas
		]

		MensagemContato.objects.bulk_create(mensagens)
		messages.success(self.request, f'{len(mensagens)} mensagem(ns) enfileirada(s) com sucesso.')
		return super().form_valid(form)


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


class PrimeiroContatoDetailView(LoginRequiredMixin, DetailView):
	template_name = 'pessoa_detail.html'
	model = PrimeiroContato
	context_object_name = 'pessoa'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context['interacoes'] = self.object.interacoes.all()
		context['interacao_form'] = kwargs.get('interacao_form', InteracaoAcolhimentoForm())
		context['tem_novo_retorno'] = self.object.mensagens.filter(
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			visualizada_equipe_em__isnull=True,
		).exists()

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
		return redirect('pessoas-detalhe', pk=pessoa.pk)


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

			if (
				mensagem.canal == MensagemContato.CanalChoices.WHATSAPP
				and not pessoa_pode_receber_whatsapp(pessoa)
			):
				messages.error(request, WHATSAPP_OPTIN_REQUIRED_ERROR)
				return redirect('pessoas-mensagens', pk=pessoa.pk)

			mensagem.save()
			messages.success(request, 'Mensagem enfileirada com sucesso.')
			return redirect('pessoas-mensagens', pk=pessoa.pk)

		messages.error(request, 'Nao foi possivel enfileirar a mensagem. Verifique os campos.')
		for campo, erros in form.errors.items():
			for erro in erros:
				messages.error(request, f'{campo}: {erro}')
		return redirect('pessoas-mensagens', pk=pessoa.pk)


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
