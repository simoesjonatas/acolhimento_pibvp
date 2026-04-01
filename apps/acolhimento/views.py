import csv

from django.contrib import messages
from django.http import HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, FormView, ListView, TemplateView, UpdateView

from apps.acolhimento.forms import AutoCadastroPrimeiroContatoForm, DisparoMensagemMassaForm, EnfileirarMensagemForm, InteracaoAcolhimentoForm, PrimeiroContatoForm
from apps.acolhimento.models import MensagemContato, PrimeiroContato


class MensagensPermissaoMixin(UserPassesTestMixin):
	raise_exception = True

	def test_func(self):
		return self.request.user.is_staff or self.request.user.is_superuser


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
		context['status_choices'] = MensagemContato.StatusFilaChoices.choices
		context['canal_choices'] = MensagemContato.CanalChoices.choices
		context['direcao_choices'] = MensagemContato.DirecaoChoices.choices
		context['sort_coluna_atual'] = sort_coluna_atual
		context['sort_direcao_atual'] = sort_direcao_atual
		context['sort_links'] = sort_links
		context['total_filtrado'] = context['paginator'].count
		context['filtro_query'] = filtro_query
		return context


class DisparoMensagemMassaView(LoginRequiredMixin, MensagensPermissaoMixin, FormView):
	template_name = 'mensagens_disparo_massa.html'
	form_class = DisparoMensagemMassaForm
	success_url = reverse_lazy('mensagens-disparo-massa')

	def get_pessoas_queryset(self):
		queryset = PrimeiroContato.objects.select_related('responsavel_atual').all()

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
		context['status_choices'] = PrimeiroContato.StatusAcolhimento.choices
		context['origem_choices'] = PrimeiroContato.OrigemCadastroChoices.choices
		context['responsaveis_choices'] = responsaveis_choices
		context['total_pessoas_filtradas'] = queryset_filtrado.count()
		context['selected_pessoas'] = selected_pessoas
		return context

	def form_valid(self, form):
		pessoas = form.cleaned_data['pessoas']
		canal = form.cleaned_data['canal']
		conteudo = form.cleaned_data['conteudo']

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


class PrimeiroContatoMensagensView(LoginRequiredMixin, MensagensPermissaoMixin, DetailView):
	template_name = 'pessoa_mensagens.html'
	model = PrimeiroContato
	context_object_name = 'pessoa'

	def get_context_data(self, **kwargs):
		context = super().get_context_data(**kwargs)
		context['mensagens'] = self.object.mensagens.select_related('campanha').order_by('-enfileirada_em')[:50]
		context['enfileirar_mensagem_form'] = kwargs.get('enfileirar_mensagem_form', EnfileirarMensagemForm())
		return context


class PrimeiroContatoEnfileirarMensagemView(LoginRequiredMixin, MensagensPermissaoMixin, View):
	def post(self, request, pk, *args, **kwargs):
		pessoa = get_object_or_404(PrimeiroContato, pk=pk)
		form = EnfileirarMensagemForm(request.POST)

		if form.is_valid():
			mensagem = form.save(commit=False)
			mensagem.pessoa = pessoa
			mensagem.criado_por = request.user
			mensagem.prioridade = 5
			mensagem.agendada_para = None
			mensagem.direcao = MensagemContato.DirecaoChoices.SAIDA
			mensagem.status_fila = MensagemContato.StatusFilaChoices.PENDENTE
			mensagem.save()
			messages.success(request, 'Mensagem enfileirada com sucesso.')
			return redirect('pessoas-mensagens', pk=pessoa.pk)

		messages.error(request, 'Nao foi possivel enfileirar a mensagem. Verifique os campos.')
		for campo, erros in form.errors.items():
			for erro in erros:
				messages.error(request, f'{campo}: {erro}')
		return redirect('pessoas-mensagens', pk=pessoa.pk)


class MensagemContatoExcluirView(LoginRequiredMixin, MensagensPermissaoMixin, View):
	def post(self, request, pk, *args, **kwargs):
		mensagem = get_object_or_404(MensagemContato.objects.select_related('pessoa'), pk=pk)
		next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or reverse('mensagens-fila')
		if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
			next_url = reverse('mensagens-fila')

		if mensagem.status_fila != MensagemContato.StatusFilaChoices.PENDENTE:
			messages.error(request, 'A mensagem so pode ser excluida enquanto estiver pendente.')
			return redirect(next_url)

		mensagem.delete()
		messages.success(request, 'Mensagem excluida com sucesso.')
		return redirect(next_url)


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
