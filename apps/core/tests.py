from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.core.forms import PERMISSAO_CONVERSAR_PESSOAS, UsuarioCreateForm


User = get_user_model()


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
