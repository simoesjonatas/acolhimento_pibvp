from django.contrib import admin

from apps.acolhimento.models import CampanhaComunicacao, ExecucaoProcessamentoFila, InteracaoAcolhimento, MensagemContato, PrimeiroContato


@admin.register(PrimeiroContato)
class PrimeiroContatoAdmin(admin.ModelAdmin):
	list_display = (
		'nome',
		'telefone_whatsapp',
		'primeira_vez',
		'origem_cadastro',
		'criado_por',
		'responsavel_atual',
		'status',
		'data_primeiro_contato',
	)
	list_filter = ('origem_cadastro', 'responsavel_atual', 'status', 'data_primeiro_contato')
	search_fields = ('nome', 'telefone_whatsapp', 'email', 'responsavel_atual__username', 'responsavel_atual__first_name', 'responsavel_atual__last_name')


@admin.register(InteracaoAcolhimento)
class InteracaoAcolhimentoAdmin(admin.ModelAdmin):
	list_display = ('pessoa', 'tipo', 'data_interacao', 'criado_em')
	list_filter = ('tipo', 'data_interacao')
	search_fields = ('pessoa__nome', 'descricao')


@admin.register(CampanhaComunicacao)
class CampanhaComunicacaoAdmin(admin.ModelAdmin):
	list_display = (
		'titulo',
		'canal',
		'status',
		'agendada_para',
		'criado_por',
		'criado_em',
	)
	list_filter = ('canal', 'status', 'agendada_para', 'criado_em')
	search_fields = ('titulo', 'descricao', 'publico_alvo_descricao', 'resultado')
	filter_horizontal = ('contatos_alvo',)


@admin.register(MensagemContato)
class MensagemContatoAdmin(admin.ModelAdmin):
	list_display = (
		'pessoa',
		'canal',
		'direcao',
		'status_fila',
		'agendada_para',
		'enviada_em',
		'referencia_externa',
	)
	list_filter = ('canal', 'direcao', 'status_fila', 'agendada_para', 'enviada_em')
	search_fields = ('pessoa__nome', 'pessoa__telefone_whatsapp', 'conteudo', 'referencia_externa', 'resposta_conteudo')
	readonly_fields = ('enfileirada_em', 'atualizado_em')


@admin.register(ExecucaoProcessamentoFila)
class ExecucaoProcessamentoFilaAdmin(admin.ModelAdmin):
	list_display = ('id', 'status', 'limite', 'dry_run', 'total_selecionado', 'total_sucesso', 'total_falha', 'iniciado_em', 'finalizado_em')
	list_filter = ('status', 'dry_run', 'iniciado_em')
	search_fields = ('id', 'solicitado_por__username', 'solicitado_por__first_name', 'solicitado_por__last_name')
	readonly_fields = ('iniciado_em', 'finalizado_em', 'atualizado_em')
