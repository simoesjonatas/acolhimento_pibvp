from django.db import models
from django.core.validators import RegexValidator
from django.conf import settings
from django.utils import timezone


class PrimeiroContato(models.Model):
	class OrigemCadastroChoices(models.TextChoices):
		EQUIPE = 'equipe', 'Equipe'
		AUTO_CADASTRO = 'auto_cadastro', 'Auto cadastro'

	class StatusAcolhimento(models.TextChoices):
		PRIMEIRO_CONTATO = 'primeiro_contato', 'Primeiro contato'
		EM_ACOMPANHAMENTO = 'em_acompanhamento', 'Em acompanhamento'
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

	telefone_whatsapp_validator = RegexValidator(
		regex=r'^\(?[1-9]{2}\)?\s?9?\d{4}-?\d{4}$',
		message='Informe um telefone/WhatsApp valido com DDD. Ex.: (31) 99999-9999',
	)

	nome = models.CharField(max_length=150)
	telefone_whatsapp = models.CharField(max_length=20, validators=[telefone_whatsapp_validator])
	primeira_vez = models.BooleanField(default=True)
	como_conheceu = models.CharField(max_length=20, choices=ComoConheceuChoices.choices)
	o_que_busca = models.CharField(max_length=25, choices=OQueBuscaChoices.choices)
	origem_cadastro = models.CharField(
		max_length=20,
		choices=OrigemCadastroChoices.choices,
		default=OrigemCadastroChoices.EQUIPE,
	)
	criado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='cadastros_primeiro_contato',
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

	def __str__(self):
		return f'{self.nome} ({self.get_status_display()})'


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
	criado_em = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ['-data_interacao', '-criado_em']
		verbose_name = 'Interação de acolhimento'
		verbose_name_plural = 'Interações de acolhimento'

	def __str__(self):
		return f'{self.pessoa.nome} - {self.get_tipo_display()}'
