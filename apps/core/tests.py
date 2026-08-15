from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.forms import PERMISSAO_CONVERSAR_PESSOAS, UsuarioCreateForm
from apps.core.models import QrCodeDinamico


User = get_user_model()

TEST_STORAGES = {
	'default': {
		'BACKEND': 'django.core.files.storage.FileSystemStorage',
	},
	'staticfiles': {
		'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
	},
}


class LoginPorUsuarioOuEmailTests(TestCase):
	def setUp(self):
		self.user = User.objects.create_user(
			username='ana', email='ana@pibvp.com.br', password='segredo123'
		)

	def test_login_por_username_em_maiusculo(self):
		self.assertTrue(self.client.login(username='ANA', password='segredo123'))

	def test_login_por_email_case_insensitive(self):
		self.assertTrue(self.client.login(username='Ana@PIBVP.com.br', password='segredo123'))

	def test_login_senha_errada_falha(self):
		self.assertFalse(self.client.login(username='ana', password='errada'))

	def test_login_usuario_inexistente_falha(self):
		self.assertFalse(self.client.login(username='ninguem', password='segredo123'))


class PaginaNaoEncontradaTests(TestCase):
	@override_settings(DEBUG=False, STORAGES=TEST_STORAGES)
	def test_404_publico_usa_template_personalizado(self):
		response = self.client.get('/rota-inexistente/')

		self.assertEqual(response.status_code, 404)
		self.assertTemplateUsed(response, '404.html')
		self.assertContains(response, 'Pagina nao encontrada', status_code=404)
		self.assertContains(response, 'Entrar no sistema', status_code=404)

	@override_settings(DEBUG=False, STORAGES=TEST_STORAGES)
	def test_404_autenticado_exibe_atalhos_da_aplicacao(self):
		user = User.objects.create_user(username='ana', password='segredo123')
		self.client.force_login(user)

		response = self.client.get('/rota-inexistente/')

		self.assertEqual(response.status_code, 404)
		self.assertTemplateUsed(response, '404.html')
		self.assertContains(response, 'Ir para dashboard', status_code=404)
		self.assertContains(response, 'Ver pessoas', status_code=404)


class UsernameMinusculoTests(TestCase):
	def test_signal_grava_username_minusculo(self):
		user = User.objects.create_user(username='JoaoSilva', password='x')
		user.refresh_from_db()
		self.assertEqual(user.username, 'joaosilva')

	def test_signal_grava_email_minusculo(self):
		user = User.objects.create_user(username='carlos', email='Carlos.Souza@PIBVP.COM.BR', password='x')
		user.refresh_from_db()
		self.assertEqual(user.email, 'carlos.souza@pibvp.com.br')

	def test_form_create_normaliza_username(self):
		form = UsuarioCreateForm(data={
			'username': 'MariaSouza',
			'first_name': 'Maria',
			'last_name': 'Souza',
			'email': 'maria@x.com',
			'is_active': True,
			'is_staff': False,
			'password1': 'SenhaForte1',
			'password2': 'SenhaForte1',
		})
		self.assertTrue(form.is_valid(), form.errors)
		user = form.save()
		self.assertEqual(user.username, 'mariasouza')

	def test_form_create_salva_permissao_de_mensagens_das_pessoas(self):
		form = UsuarioCreateForm(data={
			'username': 'mensageiro',
			'first_name': 'Mensageiro',
			'last_name': '',
			'email': 'mensageiro@x.com',
			'is_active': True,
			'pode_conversar_pessoas': True,
			'is_staff': False,
			'password1': 'SenhaForte1',
			'password2': 'SenhaForte1',
		})
		self.assertTrue(form.is_valid(), form.errors)
		user = form.save()

		self.assertTrue(user.has_perm(PERMISSAO_CONVERSAR_PESSOAS))
		self.assertFalse(user.is_staff)

	def test_form_rejeita_username_duplicado_case_insensitive(self):
		User.objects.create_user(username='pedro', password='x')
		form = UsuarioCreateForm(data={
			'username': 'PEDRO',
			'first_name': '',
			'last_name': '',
			'email': '',
			'is_active': True,
			'is_staff': False,
			'password1': 'SenhaForte1',
			'password2': 'SenhaForte1',
		})
		self.assertFalse(form.is_valid())
		self.assertIn('username', form.errors)


@override_settings(STORAGES=TEST_STORAGES)
class QrCodeDinamicoTests(TestCase):
	def setUp(self):
		self.admin = User.objects.create_superuser(
			username='admin', email='admin@pibvp.com.br', password='segredo123'
		)
		self.comum = User.objects.create_user(
			username='joao', email='joao@pibvp.com.br', password='segredo123'
		)
		self.qr = QrCodeDinamico.objects.create(
			nome='Cartaz entrada', destino='https://example.com/inicial'
		)
		self.qr_inativo = QrCodeDinamico.objects.create(
			nome='Cartaz antigo', destino='https://example.com/antigo', ativo=False
		)

	def test_codigo_gerado_automaticamente_e_unico(self):
		self.assertEqual(len(self.qr.codigo), 8)
		self.assertNotEqual(self.qr.codigo, self.qr_inativo.codigo)

	def test_redirect_ativo_302_e_incrementa_contador(self):
		url = reverse('qr-redirect', args=[self.qr.codigo])

		response = self.client.get(url)

		self.assertEqual(response.status_code, 302)
		self.assertEqual(response['Location'], 'https://example.com/inicial')
		self.assertEqual(response['Cache-Control'], 'no-store, max-age=0')

		self.qr.refresh_from_db()
		self.assertEqual(self.qr.total_acessos, 1)
		self.assertIsNotNone(self.qr.ultimo_acesso)

	def test_redirect_segue_destino_atualizado_sem_trocar_codigo(self):
		codigo_original = self.qr.codigo
		self.qr.destino = 'https://example.com/novo-destino'
		self.qr.save()

		self.assertEqual(self.qr.codigo, codigo_original)
		response = self.client.get(reverse('qr-redirect', args=[codigo_original]))
		self.assertEqual(response['Location'], 'https://example.com/novo-destino')

	def test_redirect_inativo_retorna_404(self):
		response = self.client.get(reverse('qr-redirect', args=[self.qr_inativo.codigo]))

		self.assertEqual(response.status_code, 404)
		self.assertTemplateUsed(response, 'qrcode_inativo.html')

	def test_redirect_codigo_inexistente_retorna_404(self):
		response = self.client.get(reverse('qr-redirect', args=['naoexiste']))
		self.assertEqual(response.status_code, 404)

	def test_lista_bloqueia_anonimo(self):
		# Mesmo padrao das demais telas de admin (UsuarioGestaoPermissaoMixin):
		# raise_exception=True faz o acesso sem permissao retornar 403.
		response = self.client.get(reverse('qrcodes-lista'))
		self.assertEqual(response.status_code, 403)

	def test_lista_bloqueia_usuario_comum(self):
		self.client.force_login(self.comum)
		response = self.client.get(reverse('qrcodes-lista'))
		self.assertEqual(response.status_code, 403)

	def test_lista_permite_superusuario(self):
		self.client.force_login(self.admin)
		response = self.client.get(reverse('qrcodes-lista'))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Cartaz entrada')

	def test_create_gera_codigo_e_registra_criador(self):
		self.client.force_login(self.admin)

		response = self.client.post(reverse('qrcodes-novo'), data={
			'nome': 'Boletim domingo',
			'destino': 'https://example.com/boletim',
			'ativo': 'on',
			'descricao': '',
		})

		novo = QrCodeDinamico.objects.get(nome='Boletim domingo')
		self.assertRedirects(response, reverse('qrcodes-detalhe', args=[novo.pk]))
		self.assertTrue(novo.codigo)
		self.assertEqual(novo.criado_por, self.admin)

	def test_png_download_para_superusuario(self):
		self.client.force_login(self.admin)
		response = self.client.get(reverse('qrcodes-png', args=[self.qr.pk]))

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response['Content-Type'], 'image/png')
		self.assertTrue(response.content.startswith(b'\x89PNG'))
