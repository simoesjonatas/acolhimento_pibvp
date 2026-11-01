import random
import uuid
from datetime import timedelta

from django.db import models
from django.core.validators import RegexValidator
from django.conf import settings
from django.utils import timezone
from django.utils.text import slugify


def hora_local_atual():
	return timezone.localtime().time().replace(microsecond=0)


class PrimeiroContato(models.Model):
	class OrigemCadastroChoices(models.TextChoices):
		EQUIPE = 'equipe', 'Equipe'
		AUTO_CADASTRO = 'auto_cadastro', 'Auto cadastro'

	class StatusAcolhimento(models.TextChoices):
		# A ordem de declaracao representa a ordem evolutiva do acolhimento.
		# ROBO e PARTICIPANTE mudaram so o nome na tela (Visitante / Ingressante): o
		# valor gravado no banco continua o mesmo, por isso nao ha migracao de dados.
		PRIMEIRO_CONTATO = 'primeiro_contato', 'Primeiro contato'
		ROBO = 'robo', 'Visitante'
		EM_ACOMPANHAMENTO = 'em_acompanhamento', 'Em acompanhamento'
		PARTICIPANTE = 'participante', 'Ingressante'
		APROVADO = 'aprovado', 'Aprovado'
		MEMBRO = 'membro', 'Membro'

	class GeneroChoices(models.TextChoices):
		MASCULINO = 'masculino', 'Masculino'
		FEMININO = 'feminino', 'Feminino'
		OUTRO = 'outro', 'Outro'

	class EstadoCivilChoices(models.TextChoices):
		SOLTEIRO = 'solteiro', 'Solteiro(a)'
		CASADO = 'casado', 'Casado(a)'
		UNIAO_ESTAVEL = 'uniao_estavel', 'Uniao estavel'
		DIVORCIADO = 'divorciado', 'Divorciado(a)'
		VIUVO = 'viuvo', 'Viuvo(a)'

	class ComoConheceuChoices(models.TextChoices):
		INSTAGRAM = 'instagram', 'Instagram'
		INDICACAO = 'indicacao', 'Indicacao'
		PASSANDO_NA_RUA = 'passando_na_rua', 'Passando na rua'
		EVENTO = 'evento', 'Evento'
		OUTRO = 'outro', 'Outro'

	class OQueBuscaChoices(models.TextChoices):
		CONHECER_DEUS = 'conhecer_deus', 'Conhecer Deus'
		FAZER_AMIZADES = 'fazer_amizades', 'Fazer amizades'
		RESTAURAR_VIDA = 'restaurar_vida', 'Restaurar vida'
		APOIO_EMOCIONAL = 'apoio_emocional', 'Apoio emocional'
		PARTICIPAR_DE_ALGO = 'participar_de_algo', 'Participar de algo'

	class BotEtapaChoices(models.TextChoices):
		# Estado da conversa no atendimento automatico (bot de menu).
		INATIVO = 'inativo', 'Inativo'
		MENU = 'menu', 'Aguardando escolha no menu'

	telefone_whatsapp_validator = RegexValidator(
		regex=r'^\(?[1-9]{2}\)?\s?9?\d{4}-?\d{4}$',
		message='Informe um telefone/WhatsApp valido com DDD. Ex.: (31) 99999-9999',
	)

	nome = models.CharField(max_length=150)
	telefone_whatsapp = models.CharField(max_length=20, validators=[telefone_whatsapp_validator])
	# Identidade LID do WhatsApp (ex.: '48722243260432@lid'). Desde a migracao para LID,
	# o WhatsApp rejeita (erro 463) envios enderecados ao telefone de quem ja migrou; e
	# preciso enviar para o LID. Aprendemos o valor no webhook, via `key.remoteJidAlt`.
	whatsapp_lid = models.CharField(max_length=40, blank=True, db_index=True)
	primeira_vez = models.BooleanField(default=True)
	# As opcoes validas vem de OpcaoComoConheceu (cadastro do administrador); aqui
	# guardamos o `valor` da opcao escolhida. ComoConheceuChoices ficou so como
	# constantes para o codigo e rotulo de reserva.
	como_conheceu = models.CharField(max_length=60)
	o_que_busca = models.CharField(max_length=25, choices=OQueBuscaChoices.choices)
	origem_cadastro = models.CharField(
		max_length=20,
		choices=OrigemCadastroChoices.choices,
		default=OrigemCadastroChoices.EQUIPE,
	)
	iniciou_interacao = models.BooleanField(default=False)
	bot_etapa = models.CharField(
		max_length=20,
		choices=BotEtapaChoices.choices,
		default=BotEtapaChoices.INATIVO,
	)
	criado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='cadastros_primeiro_contato',
	)
	responsavel_atual = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='primeiros_contatos_responsaveis',
	)
	email = models.EmailField(blank=True)
	genero = models.CharField(max_length=20, choices=GeneroChoices.choices, blank=True)
	idade = models.PositiveIntegerField(null=True, blank=True)
	religiao = models.CharField(max_length=100, blank=True)
	estado_civil = models.CharField(max_length=20, choices=EstadoCivilChoices.choices, blank=True)
	cidade = models.CharField(max_length=120, blank=True)
	observacoes = models.TextField(blank=True)
	status = models.CharField(
		max_length=30,
		choices=StatusAcolhimento.choices,
		default=StatusAcolhimento.PRIMEIRO_CONTATO,
	)
	data_primeiro_contato = models.DateField(auto_now_add=True)
	criado_em = models.DateTimeField(auto_now_add=True)
	atualizado_em = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['-criado_em']
		verbose_name = 'Primeiro contato'
		verbose_name_plural = 'Primeiros contatos'
		permissions = [
			('pode_conversar_pessoas', 'Pode ver e enviar mensagens das pessoas'),
		]

	def __str__(self):
		return f'{self.nome} ({self.get_status_display()})'

	def get_como_conheceu_display(self):
		# Substitui o get_FOO_display automatico (o campo nao tem mais `choices`).
		return OpcaoComoConheceu.rotulo_de(self.como_conheceu)


class CampanhaComunicacao(models.Model):
	class StatusCampanhaChoices(models.TextChoices):
		RASCUNHO = 'rascunho', 'Rascunho'
		AGENDADA = 'agendada', 'Agendada'
		EM_EXECUCAO = 'em_execucao', 'Em execução'
		CONCLUIDA = 'concluida', 'Concluída'
		CANCELADA = 'cancelada', 'Cancelada'

	class CanalChoices(models.TextChoices):
		WHATSAPP = 'whatsapp', 'WhatsApp'
		EMAIL = 'email', 'E-mail'

	titulo = models.CharField(max_length=150)
	descricao = models.TextField(blank=True)
	canal = models.CharField(max_length=20, choices=CanalChoices.choices, default=CanalChoices.WHATSAPP)
	publico_alvo_descricao = models.TextField(blank=True)
	contatos_alvo = models.ManyToManyField(
		PrimeiroContato,
		blank=True,
		related_name='campanhas_comunicacao',
	)
	agendada_para = models.DateTimeField(null=True, blank=True)
	iniciada_em = models.DateTimeField(null=True, blank=True)
	finalizada_em = models.DateTimeField(null=True, blank=True)
	status = models.CharField(
		max_length=20,
		choices=StatusCampanhaChoices.choices,
		default=StatusCampanhaChoices.RASCUNHO,
	)
	resultado = models.TextField(blank=True)
	criado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='campanhas_criadas',
	)
	criado_em = models.DateTimeField(auto_now_add=True)
	atualizado_em = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['-criado_em']
		verbose_name = 'Campanha de comunicação'
		verbose_name_plural = 'Campanhas de comunicação'

	def __str__(self):
		return self.titulo


class MensagemContato(models.Model):
	class CanalChoices(models.TextChoices):
		WHATSAPP = 'whatsapp', 'WhatsApp'
		EMAIL = 'email', 'E-mail'

	class DirecaoChoices(models.TextChoices):
		SAIDA = 'saida', 'Saida'
		ENTRADA = 'entrada', 'Entrada'

	class StatusFilaChoices(models.TextChoices):
		PENDENTE = 'pendente', 'Pendente'
		AGENDADA = 'agendada', 'Agendada'
		PROCESSANDO = 'processando', 'Processando'
		ENVIADA = 'enviada', 'Enviada'
		FALHA = 'falha', 'Falha'
		CANCELADA = 'cancelada', 'Cancelada'

	pessoa = models.ForeignKey(
		PrimeiroContato,
		on_delete=models.CASCADE,
		related_name='mensagens',
	)
	campanha = models.ForeignKey(
		CampanhaComunicacao,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='mensagens',
	)
	criado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='mensagens_criadas',
	)
	canal = models.CharField(max_length=20, choices=CanalChoices.choices, default=CanalChoices.WHATSAPP)
	direcao = models.CharField(max_length=20, choices=DirecaoChoices.choices, default=DirecaoChoices.SAIDA)
	status_fila = models.CharField(
		max_length=20,
		choices=StatusFilaChoices.choices,
		default=StatusFilaChoices.PENDENTE,
	)
	prioridade = models.PositiveSmallIntegerField(default=5)
	conteudo = models.TextField()
	referencia_externa = models.CharField(max_length=120, blank=True)
	tentativas_envio = models.PositiveSmallIntegerField(default=0)
	erro_ultimo_envio = models.TextField(blank=True)
	agendada_para = models.DateTimeField(null=True, blank=True)
	enfileirada_em = models.DateTimeField(auto_now_add=True)
	enviada_em = models.DateTimeField(null=True, blank=True)
	entregue_em = models.DateTimeField(null=True, blank=True)
	lida_em = models.DateTimeField(null=True, blank=True)
	visualizada_equipe_em = models.DateTimeField(null=True, blank=True)
	resposta_recebida_em = models.DateTimeField(null=True, blank=True)
	resposta_conteudo = models.TextField(blank=True)
	metadata_envio = models.JSONField(default=dict, blank=True)
	metadata_resposta = models.JSONField(default=dict, blank=True)
	# Cobranca informada pela Meta no webhook de status (objeto `pricing`). So existe
	# para mensagens enviadas pela API oficial; vazio = ainda sem confirmacao.
	cobranca_categoria = models.CharField(max_length=40, blank=True, db_index=True)
	cobranca_tipo = models.CharField(max_length=40, blank=True)
	cobranca_modelo = models.CharField(max_length=10, blank=True)
	cobranca_registrada_em = models.DateTimeField(null=True, blank=True)
	atualizado_em = models.DateTimeField(auto_now=True)

	@property
	def cobravel(self):
		"""True/False quando a Meta ja informou; None enquanto nao ha confirmacao."""
		if not self.cobranca_tipo and not self.cobranca_categoria:
			return None
		return self.cobranca_tipo == 'regular'

	class Meta:
		ordering = ['status_fila', 'prioridade', 'agendada_para', 'enfileirada_em']
		verbose_name = 'Mensagem de contato'
		verbose_name_plural = 'Mensagens de contato'
		indexes = [
			models.Index(fields=['status_fila', 'agendada_para']),
			models.Index(fields=['pessoa', 'enviada_em']),
			models.Index(fields=['direcao', 'canal']),
		]

	def __str__(self):
		return f'{self.pessoa.nome} - {self.get_status_fila_display()}'


class InteracaoAcolhimento(models.Model):
	class TipoInteracao(models.TextChoices):
		TENTATIVA_CONTATO = 'tentativa_contato', 'Tentativa de contato'
		RESPOSTA_RECEBIDA = 'resposta_recebida', 'Resposta recebida'
		VISITA_AGENDADA = 'visita_agendada', 'Visita agendada'
		VISITA_REALIZADA = 'visita_realizada', 'Visita realizada'
		OBSERVACAO = 'observacao', 'Observação'

	pessoa = models.ForeignKey(
		PrimeiroContato,
		on_delete=models.CASCADE,
		related_name='interacoes',
	)
	tipo = models.CharField(max_length=30, choices=TipoInteracao.choices)
	descricao = models.TextField(blank=True)
	data_interacao = models.DateField(default=timezone.localdate)
	hora_interacao = models.TimeField(default=hora_local_atual)
	criado_em = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ['-data_interacao', '-hora_interacao', '-criado_em']
		verbose_name = 'Interação de acolhimento'
		verbose_name_plural = 'Interações de acolhimento'

	def __str__(self):
		return f'{self.pessoa.nome} - {self.get_tipo_display()}'


class RegistroGabinete(models.Model):
	"""Gabinete pastoral: atendimento reservado da pessoa com o pastor.

	Fica fora da timeline (InteracaoAcolhimento) de proposito, porque a timeline e vista
	por toda a equipe. So administradores e quem tem o perfil Pastor (permissao
	`pode_ver_gabinete`) enxergam e registram o gabinete.
	"""

	class Tipo(models.TextChoices):
		AGENDADO = 'agendado', 'Gabinete agendado'
		REALIZADO = 'realizado', 'Gabinete realizado'

	pessoa = models.ForeignKey(
		PrimeiroContato,
		on_delete=models.CASCADE,
		related_name='registros_gabinete',
	)
	tipo = models.CharField(max_length=20, choices=Tipo.choices)
	data = models.DateField(default=timezone.localdate)
	hora = models.TimeField(default=hora_local_atual)
	observacoes = models.TextField(blank=True, verbose_name='Observacoes')
	criado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='registros_gabinete',
	)
	criado_em = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ['-data', '-hora', '-criado_em']
		verbose_name = 'Registro de gabinete'
		verbose_name_plural = 'Registros de gabinete'
		permissions = [
			('pode_ver_gabinete', 'Perfil pastor: pode ver e registrar o gabinete pastoral'),
		]

	def __str__(self):
		return f'{self.pessoa.nome} - {self.get_tipo_display()}'


class ExecucaoProcessamentoFila(models.Model):
	class StatusExecucaoChoices(models.TextChoices):
		EXECUTANDO = 'executando', 'Executando'
		CONCLUIDA = 'concluida', 'Concluida'
		INTERROMPIDA = 'interrompida', 'Interrompida'
		FALHA = 'falha', 'Falha'

	class OrigemChoices(models.TextChoices):
		MANUAL = 'manual', 'Manual'
		AUTOMATICO = 'automatico', 'Automatico'

	solicitado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='execucoes_fila_mensagens',
	)
	status = models.CharField(
		max_length=20,
		choices=StatusExecucaoChoices.choices,
		default=StatusExecucaoChoices.EXECUTANDO,
	)
	limite = models.PositiveIntegerField(default=20)
	dry_run = models.BooleanField(default=False)
	origem = models.CharField(
		max_length=20,
		choices=OrigemChoices.choices,
		default=OrigemChoices.MANUAL,
	)
	ids_filtrados = models.JSONField(default=list, blank=True)
	total_selecionado = models.PositiveIntegerField(default=0)
	total_processado = models.PositiveIntegerField(default=0)
	total_sucesso = models.PositiveIntegerField(default=0)
	total_falha = models.PositiveIntegerField(default=0)
	solicitar_parada = models.BooleanField(default=False)
	log_execucao = models.TextField(blank=True)
	iniciado_em = models.DateTimeField(auto_now_add=True)
	finalizado_em = models.DateTimeField(null=True, blank=True)
	atualizado_em = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['-iniciado_em']
		verbose_name = 'Execucao de processamento da fila'
		verbose_name_plural = 'Execucoes de processamento da fila'

	def __str__(self):
		return f'Execucao {self.pk} - {self.get_status_display()}'


class ConfiguracaoProcessamentoFila(models.Model):
	"""Singleton (pk=1): switch + ritmo anti-bloqueio do processamento da fila.

	Os campos de ritmo espalham os envios no tempo para nao parecer disparo em
	massa (o que faz o WhatsApp bloquear a conta). Defaults calibrados no modo
	"seguro" (~1 msg a cada 60-120s, em horario comercial, com 'digitando...').
	"""

	auto_ativo = models.BooleanField(default=False)
	# Intervalo entre uma mensagem e a proxima, sorteado nesta faixa (segundos).
	intervalo_min_seg = models.PositiveIntegerField(default=60)
	intervalo_max_seg = models.PositiveIntegerField(default=120)
	# Teto de mensagens ENVIADAS por dia (0 = sem limite). Comece baixo (20-30) ao
	# "esquentar" um numero novo e aumente aos poucos.
	teto_diario = models.PositiveIntegerField(default=0)
	# Janela de horario permitido (hora local, 0-23). Evita disparo de madrugada.
	janela_envio_inicio = models.PositiveSmallIntegerField(default=8)
	janela_envio_fim = models.PositiveSmallIntegerField(default=21)
	# "Digitando..." (composing) que a Evolution simula antes de enviar (ms).
	delay_digitando_min_ms = models.PositiveIntegerField(default=1200)
	delay_digitando_max_ms = models.PositiveIntegerField(default=3500)
	atualizado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='+',
	)
	atualizado_em = models.DateTimeField(auto_now=True)

	class Meta:
		verbose_name = 'Configuracao do processamento da fila'
		verbose_name_plural = 'Configuracao do processamento da fila'

	def __str__(self):
		return 'Processamento automatico: ' + ('ligado' if self.auto_ativo else 'desligado')

	@classmethod
	def carregar(cls):
		obj, _created = cls.objects.get_or_create(pk=1)
		return obj

	@classmethod
	def auto_ligado(cls) -> bool:
		return cls.objects.filter(pk=1, auto_ativo=True).exists()

	def intervalo_aleatorio_seg(self) -> int:
		"""Segundos ate a proxima mensagem (jitter aleatorio na faixa configurada)."""
		minimo = max(int(self.intervalo_min_seg or 0), 0)
		maximo = max(int(self.intervalo_max_seg or 0), minimo)
		return random.randint(minimo, maximo)

	def delay_digitando_ms(self) -> int:
		"""Milissegundos de 'digitando...' antes do envio (jitter aleatorio)."""
		minimo = max(int(self.delay_digitando_min_ms or 0), 0)
		maximo = max(int(self.delay_digitando_max_ms or 0), minimo)
		return random.randint(minimo, maximo)

	def _janela_restringe(self) -> bool:
		inicio = int(self.janela_envio_inicio or 0)
		fim = int(self.janela_envio_fim or 0)
		return 0 <= inicio < fim <= 24

	def proximo_horario_valido(self, dt):
		"""Empurra `dt` (aware) para dentro da janela de horario permitido (hora local).

		Se a janela estiver desligada/invalida, devolve `dt` sem alterar. Se `dt`
		cair antes do inicio, adianta para o inicio do mesmo dia; se cair depois do
		fim, joga para o inicio da janela no dia seguinte.
		"""
		if not self._janela_restringe():
			return dt
		inicio = int(self.janela_envio_inicio)
		fim = int(self.janela_envio_fim)
		local = timezone.localtime(dt)
		if local.hour < inicio:
			local = local.replace(hour=inicio, minute=0, second=0, microsecond=0)
		elif local.hour >= fim:
			local = (local + timedelta(days=1)).replace(
				hour=inicio, minute=0, second=0, microsecond=0
			)
		return local


class Questionario(models.Model):
	# Titulo usado pelo comando `popular_questionario_membresia`. Serve de fallback
	# para achar o questionario de membresia enquanto ninguem marcou a flag.
	TITULO_MEMBRESIA = 'Formulario de Membresia'

	titulo = models.CharField(max_length=150)
	descricao = models.TextField(blank=True)
	ativo = models.BooleanField(default=True)
	padrao_membresia = models.BooleanField(
		default=False,
		verbose_name='Questionario de membresia',
		help_text='Gerado automaticamente quando a pessoa passa para o status Ingressante.',
	)
	criado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='questionarios_criados',
	)
	criado_em = models.DateTimeField(auto_now_add=True)
	atualizado_em = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['-criado_em']
		verbose_name = 'Questionario'
		verbose_name_plural = 'Questionarios'

	def __str__(self):
		return self.titulo

	def save(self, *args, **kwargs):
		super().save(*args, **kwargs)
		if self.padrao_membresia:
			# So um questionario pode ser o de membresia.
			Questionario.objects.filter(padrao_membresia=True).exclude(pk=self.pk).update(
				padrao_membresia=False
			)

	@classmethod
	def membresia(cls):
		"""Questionario gerado no automatico quando a pessoa vira Participante."""
		ativos = cls.objects.filter(ativo=True)
		return (
			ativos.filter(padrao_membresia=True).first()
			or ativos.filter(titulo=cls.TITULO_MEMBRESIA).first()
		)


class PerguntaQuestionario(models.Model):
	class TipoPergunta(models.TextChoices):
		TEXTO_CURTO = 'texto_curto', 'Texto curto'
		TEXTO_LONGO = 'texto_longo', 'Texto longo'
		ESCOLHA_UNICA = 'escolha_unica', 'Escolha unica'
		DATA = 'data', 'Data'
		SIM_NAO = 'sim_nao', 'Sim / Nao'

	questionario = models.ForeignKey(
		Questionario,
		on_delete=models.CASCADE,
		related_name='perguntas',
	)
	texto = models.CharField(max_length=300)
	ajuda = models.CharField(max_length=300, blank=True)
	tipo = models.CharField(max_length=20, choices=TipoPergunta.choices, default=TipoPergunta.TEXTO_CURTO)
	obrigatoria = models.BooleanField(default=False)
	ordem = models.PositiveIntegerField(default=0)

	class Meta:
		ordering = ['ordem', 'id']
		verbose_name = 'Pergunta'
		verbose_name_plural = 'Perguntas'

	def __str__(self):
		return self.texto

	@property
	def tem_opcoes(self):
		return self.tipo == self.TipoPergunta.ESCOLHA_UNICA


class OpcaoPergunta(models.Model):
	pergunta = models.ForeignKey(
		PerguntaQuestionario,
		on_delete=models.CASCADE,
		related_name='opcoes',
	)
	texto = models.CharField(max_length=200)
	ordem = models.PositiveIntegerField(default=0)

	class Meta:
		ordering = ['ordem', 'id']
		verbose_name = 'Opcao de pergunta'
		verbose_name_plural = 'Opcoes de pergunta'

	def __str__(self):
		return self.texto


class ConviteQuestionario(models.Model):
	class StatusConvite(models.TextChoices):
		PENDENTE = 'pendente', 'Pendente'
		RESPONDIDO = 'respondido', 'Respondido'

	questionario = models.ForeignKey(
		Questionario,
		on_delete=models.CASCADE,
		related_name='convites',
	)
	pessoa = models.ForeignKey(
		PrimeiroContato,
		on_delete=models.CASCADE,
		related_name='convites_questionario',
	)
	token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
	status = models.CharField(
		max_length=20,
		choices=StatusConvite.choices,
		default=StatusConvite.PENDENTE,
	)
	criado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='convites_questionario_criados',
	)
	respondido_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='convites_questionario_respondidos',
	)
	respondente_nome = models.CharField(max_length=150, blank=True)
	criado_em = models.DateTimeField(auto_now_add=True)
	respondido_em = models.DateTimeField(null=True, blank=True)

	class Meta:
		ordering = ['-criado_em']
		verbose_name = 'Convite de questionario'
		verbose_name_plural = 'Convites de questionario'

	def __str__(self):
		return f'{self.pessoa.nome} - {self.questionario.titulo} ({self.get_status_display()})'

	@property
	def respondido(self):
		return self.status == self.StatusConvite.RESPONDIDO


class RespostaPergunta(models.Model):
	convite = models.ForeignKey(
		ConviteQuestionario,
		on_delete=models.CASCADE,
		related_name='respostas',
	)
	pergunta = models.ForeignKey(
		PerguntaQuestionario,
		on_delete=models.CASCADE,
		related_name='respostas',
	)
	valor_texto = models.TextField(blank=True)
	opcao = models.ForeignKey(
		OpcaoPergunta,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='respostas',
	)

	class Meta:
		unique_together = ('convite', 'pergunta')
		verbose_name = 'Resposta de pergunta'
		verbose_name_plural = 'Respostas de pergunta'

	def __str__(self):
		return f'{self.pergunta.texto}: {self.valor_exibicao}'

	@property
	def valor_exibicao(self):
		if self.opcao_id:
			return self.opcao.texto
		return self.valor_texto


class TemplateWhatsapp(models.Model):
	"""Configuracao das mensagens padrao do WhatsApp, editavel pela aplicacao.

	Na Twilio, guarda o Content Template SID e suas variaveis. Na API oficial da
	Meta, guarda o nome e o idioma do template aprovado (as variaveis sao as
	mesmas da Twilio). No Evolution, guarda o texto simples equivalente. Quando
	preenchido, sobrescreve o valor do .env (settings); vazio => usa o .env.
	Os valores de `tipo` batem com metadata_envio['tipo_template'] das mensagens.
	"""

	class Tipo(models.TextChoices):
		PRIMEIRO_CONTATO = 'primeiro_contato_opt_in', 'Primeiro contato (opt-in)'
		CONTINUAR = 'continuar_conversa', 'Continuar conversa'

	tipo = models.CharField(max_length=40, choices=Tipo.choices, unique=True)
	content_sid = models.CharField('Content Template SID', max_length=64, blank=True)
	content_variables = models.TextField('Variaveis (JSON)', blank=True, default='{}')
	texto_evolution = models.TextField('Texto WhatsApp', blank=True)
	meta_nome = models.CharField('Nome do template (Meta)', max_length=512, blank=True)
	meta_idioma = models.CharField('Idioma do template (Meta)', max_length=15, blank=True, default='pt_BR')
	atualizado_em = models.DateTimeField(auto_now=True)
	atualizado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='templates_whatsapp_atualizados',
	)

	class Meta:
		verbose_name = 'Template do WhatsApp'
		verbose_name_plural = 'Templates do WhatsApp'

	def __str__(self):
		return self.get_tipo_display()


class ConfiguracaoAtendimentoBot(models.Model):
	"""Singleton (pk=1): liga/desliga o atendimento automatico (bot de menu) e guarda os textos fixos."""

	ativo = models.BooleanField(default=False)
	mensagem_saudacao = models.TextField(
		'Mensagem de saudacao',
		default='Ola! Que bom falar com voce. Com o que posso te ajudar hoje?',
	)
	mensagem_fallback = models.TextField(
		'Mensagem quando nao entende',
		default='Nao entendi. Responda com o numero de uma das opcoes abaixo:',
	)
	atualizado_em = models.DateTimeField(auto_now=True)
	atualizado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='+',
	)

	class Meta:
		verbose_name = 'Configuracao do atendimento automatico'
		verbose_name_plural = 'Configuracao do atendimento automatico'

	def __str__(self):
		return 'Atendimento automatico: ' + ('ligado' if self.ativo else 'desligado')

	@classmethod
	def carregar(cls):
		obj, _created = cls.objects.get_or_create(pk=1)
		return obj

	@classmethod
	def ativo_ligado(cls) -> bool:
		return cls.objects.filter(pk=1, ativo=True).exists()


class OpcaoAtendimentoBot(models.Model):
	"""Uma opcao do menu do atendimento automatico (cadastro pelo superusuario)."""

	class Acao(models.TextChoices):
		RESPONDER = 'responder', 'So responder'
		TRANSFERIR_HUMANO = 'transferir_humano', 'Transferir para atendente'

	ordem = models.PositiveIntegerField(default=1)
	rotulo = models.CharField('Rotulo no menu', max_length=80)
	palavras_chave = models.TextField(
		'Palavras-chave',
		blank=True,
		help_text=(
			'Aliases que acionam a opcao (separados por virgula ou linha). O numero da opcao '
			'funciona quando ela aparece no menu.'
		),
	)
	resposta = models.TextField('Resposta enviada')
	acao = models.CharField(max_length=20, choices=Acao.choices, default=Acao.RESPONDER)
	# Ativa = o bot responde a opcao. Inativa = o bot a ignora por completo.
	ativa = models.BooleanField(default=True)
	# Aparece no menu = listada e numerada. Fora do menu (e ativa) = so responde pelas
	# palavras-chave, sem numero; serve para atalhos que nao precisam ficar visiveis.
	aparece_no_menu = models.BooleanField('Aparece no menu', default=True)
	atualizado_em = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['ordem', 'id']
		verbose_name = 'Opcao do atendimento automatico'
		verbose_name_plural = 'Opcoes do atendimento automatico'

	def __str__(self):
		return f'{self.ordem}. {self.rotulo}'

	def palavras_chave_lista(self) -> list[str]:
		import re
		bruto = re.split(r'[,\n;]+', self.palavras_chave or '')
		return [item.strip().lower() for item in bruto if item.strip()]


class OpcaoComoConheceu(models.Model):
	"""Uma opcao do campo "Como conheceu" (cadastro pelo superusuario).

	So as opcoes ativas aparecem no auto cadastro e no cadastro interno. O `valor`
	e o que fica gravado em PrimeiroContato.como_conheceu; ele e gerado na criacao
	e nao muda mais, entao renomear o rotulo nao quebra cadastros antigos.
	"""

	ordem = models.PositiveIntegerField(default=1)
	rotulo = models.CharField('Opcao', max_length=60)
	valor = models.CharField(max_length=60, unique=True, editable=False)
	ativa = models.BooleanField(default=True)
	atualizado_em = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ['ordem', 'id']
		verbose_name = 'Opcao de "Como conheceu"'
		verbose_name_plural = 'Opcoes de "Como conheceu"'

	def __str__(self):
		return self.rotulo

	def save(self, *args, **kwargs):
		if not self.valor:
			self.valor = self._gerar_valor()
		super().save(*args, **kwargs)

	def _gerar_valor(self) -> str:
		base = slugify(self.rotulo).replace('-', '_')[:50] or 'opcao'
		valor, sufixo = base, 2
		while OpcaoComoConheceu.objects.filter(valor=valor).exists():
			valor = f'{base}_{sufixo}'
			sufixo += 1
		return valor

	def em_uso(self) -> bool:
		return PrimeiroContato.objects.filter(como_conheceu=self.valor).exists()

	@classmethod
	def choices_ativas(cls, incluir_valor: str = '') -> list[tuple[str, str]]:
		"""Opcoes ativas como choices; `incluir_valor` mantem a opcao atual de quem ja a tem."""
		choices = list(cls.objects.filter(ativa=True).values_list('valor', 'rotulo'))
		if incluir_valor and incluir_valor not in dict(choices):
			choices.append((incluir_valor, cls.rotulo_de(incluir_valor)))
		return choices

	@classmethod
	def rotulo_de(cls, valor: str) -> str:
		if not valor:
			return ''
		rotulo = cls.objects.filter(valor=valor).values_list('rotulo', flat=True).first()
		if rotulo:
			return rotulo
		return dict(PrimeiroContato.ComoConheceuChoices.choices).get(valor, valor)


class TabelaPrecoWhatsapp(models.Model):
	"""Singleton (pk=1): preco por mensagem cobrada, por categoria, para o custo estimado.

	O custo oficial vem da propria Meta (pricing_analytics); esta tabela serve para
	estimar por origem/pessoa e quando a consulta oficial nao esta disponivel. Os
	valores devem ser copiados da tabela de precos da Meta para o Brasil.
	"""

	MOEDAS = [('BRL', 'Real (BRL)'), ('USD', 'Dolar (USD)')]

	moeda = models.CharField(max_length=3, choices=MOEDAS, default='BRL')
	preco_marketing = models.DecimalField('Marketing', max_digits=10, decimal_places=4, default=0)
	preco_utility = models.DecimalField('Utilidade', max_digits=10, decimal_places=4, default=0)
	preco_authentication = models.DecimalField('Autenticacao', max_digits=10, decimal_places=4, default=0)
	preco_service = models.DecimalField('Servico (acima da franquia)', max_digits=10, decimal_places=4, default=0)
	atualizado_em = models.DateTimeField(auto_now=True)
	atualizado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='+',
	)

	class Meta:
		verbose_name = 'Tabela de precos do WhatsApp'
		verbose_name_plural = 'Tabela de precos do WhatsApp'

	def __str__(self):
		return f'Precos WhatsApp ({self.moeda})'

	@classmethod
	def carregar(cls):
		obj, _ = cls.objects.get_or_create(pk=1)
		return obj

	def preenchida(self) -> bool:
		return any(
			valor and valor > 0
			for valor in (self.preco_marketing, self.preco_utility, self.preco_authentication, self.preco_service)
		)
