from django.contrib import admin

from apps.acolhimento.models import InteracaoAcolhimento, PrimeiroContato


@admin.register(PrimeiroContato)
class PrimeiroContatoAdmin(admin.ModelAdmin):
	list_display = (
		'nome',
		'telefone_whatsapp',
		'primeira_vez',
		'origem_cadastro',
		'criado_por',
		'status',
		'data_primeiro_contato',
	)
	list_filter = ('origem_cadastro', 'status', 'data_primeiro_contato')
	search_fields = ('nome', 'telefone_whatsapp', 'email')


@admin.register(InteracaoAcolhimento)
class InteracaoAcolhimentoAdmin(admin.ModelAdmin):
	list_display = ('pessoa', 'tipo', 'data_interacao', 'criado_em')
	list_filter = ('tipo', 'data_interacao')
	search_fields = ('pessoa__nome', 'descricao')
