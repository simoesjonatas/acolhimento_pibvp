import secrets

from django.conf import settings
from django.db import models
from django.urls import reverse


# Alfabeto sem caracteres ambiguos (0/O, 1/l/I) para o codigo ficar legivel.
CODIGO_ALFABETO = 'abcdefghjkmnpqrstuvwxyz23456789'
CODIGO_TAMANHO = 8


def gerar_codigo_qrcode(tamanho=CODIGO_TAMANHO):
	"""Gera um codigo curto e aleatorio para compor a URL fixa do QR Code."""
	return ''.join(secrets.choice(CODIGO_ALFABETO) for _ in range(tamanho))


class QrCodeDinamico(models.Model):
	"""QR Code com link fixo e destino reconfiguravel (redirecionamento dinamico).

	A ideia: a URL publica `/r/<codigo>/` e gerada uma unica vez e impressa no QR.
	Quando alguem escaneia, o sistema consulta o `destino` atual e redireciona (302).
	Assim o mesmo QR impresso pode apontar para varios lugares ao longo do tempo,
	bastando o admin trocar o destino aqui, sem reimprimir nada.
	"""

	codigo = models.CharField(
		'codigo',
		max_length=16,
		unique=True,
		editable=False,
		db_index=True,
		help_text='Identificador fixo usado na URL publica do QR Code.',
	)
	nome = models.CharField(
		'nome',
		max_length=120,
		help_text='Nome interno para identificar o QR Code (ex.: Cartaz da entrada).',
	)
	destino = models.URLField(
		'URL de destino',
		max_length=2000,
		help_text='Para onde o QR Code deve redirecionar quando for escaneado.',
	)
	ativo = models.BooleanField(
		'ativo',
		default=True,
		help_text='Se desativado, o QR Code para de redirecionar.',
	)
	descricao = models.TextField('descricao', blank=True)
	total_acessos = models.PositiveIntegerField('total de acessos', default=0, editable=False)
	ultimo_acesso = models.DateTimeField('ultimo acesso', null=True, blank=True, editable=False)
	criado_por = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.SET_NULL,
		null=True,
		blank=True,
		related_name='qrcodes_criados',
		verbose_name='criado por',
	)
	criado_em = models.DateTimeField('criado em', auto_now_add=True)
	atualizado_em = models.DateTimeField('atualizado em', auto_now=True)

	class Meta:
		verbose_name = 'QR Code'
		verbose_name_plural = 'QR Codes'
		ordering = ['-criado_em']

	def __str__(self):
		return self.nome

	def save(self, *args, **kwargs):
		# Gera um codigo unico na primeira gravacao e nunca mais o altera,
		# garantindo que o QR impresso continue valido pra sempre.
		if not self.codigo:
			codigo = gerar_codigo_qrcode()
			while QrCodeDinamico.objects.filter(codigo=codigo).exists():
				codigo = gerar_codigo_qrcode()
			self.codigo = codigo
		super().save(*args, **kwargs)

	def get_absolute_url(self):
		return reverse('qrcodes-detalhe', args=[self.pk])

	def get_public_path(self):
		"""Caminho relativo (sem dominio) da URL fixa que vai dentro do QR Code."""
		return reverse('qr-redirect', args=[self.codigo])
