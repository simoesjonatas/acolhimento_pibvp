import csv

from django.contrib import messages
from django.http import HttpResponse
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.mixins import UserPassesTestMixin
from django.db.models import Q
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from apps.acolhimento.forms import InteracaoAcolhimentoForm, PrimeiroContatoForm
from apps.acolhimento.models import PrimeiroContato


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


class PrimeiroContatoCreateView(LoginRequiredMixin, CreateView):
	template_name = 'pessoa_form.html'
	form_class = PrimeiroContatoForm
	success_url = reverse_lazy('pessoas-lista')

	def form_valid(self, form):
		form.instance.origem_cadastro = PrimeiroContato.OrigemCadastroChoices.EQUIPE
		form.instance.criado_por = self.request.user
		response = super().form_valid(form)
		messages.success(self.request, 'Pessoa cadastrada com sucesso.')
		return response

	def form_invalid(self, form):
		messages.error(self.request, 'Nao foi possivel salvar. Verifique os campos e tente novamente.')
		return super().form_invalid(form)


class AutoCadastroCreateView(CreateView):
	template_name = 'auto_cadastro_form.html'
	form_class = PrimeiroContatoForm
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
			writer.writerow([
				pessoa.nome,
				pessoa.telefone_whatsapp,
				'Sim' if pessoa.primeira_vez else 'Nao',
				pessoa.get_como_conheceu_display(),
				pessoa.get_o_que_busca_display(),
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
