from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.core.forms import PERMISSAO_CONVERSAR_PESSOAS, UsuarioCreateForm, UsuarioUpdateForm, eh_pastor
from apps.core.models import QrCodeDinamico, telefone_do_usuario


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


class PerfilPastorTests(TestCase):
	"""Perfil Pastor no cadastro de usuarios: libera o gabinete pastoral."""

	def setUp(self):
		self.admin = User.objects.create_superuser('admin_pastor', password='x')
		self.usuario = User.objects.create_user('joao', password='x')

	def _editar(self, **dados):
		self.client.force_login(self.admin)
		payload = {'username': 'joao', 'first_name': '', 'last_name': '', 'email': '', 'is_active': 'on'}
		payload.update(dados)
		return self.client.post(reverse('usuarios-editar', args=[self.usuario.pk]), payload)

	def test_form_create_salva_perfil_pastor(self):
		form = UsuarioCreateForm(data={
			'username': 'pastor',
			'first_name': 'Pastor',
			'last_name': '',
			'email': '',
			'is_active': True,
			'perfil_pastor': True,
			'is_staff': False,
			'password1': 'SenhaForte1',
			'password2': 'SenhaForte1',
		})
		self.assertTrue(form.is_valid(), form.errors)
		user = form.save()

		self.assertTrue(eh_pastor(user))
		self.assertTrue(User.objects.get(pk=user.pk).has_perm('acolhimento.pode_ver_gabinete'))
		self.assertFalse(user.is_staff)

	def test_marcar_e_desmarcar_pastor_na_edicao(self):
		self._editar(perfil_pastor='on')
		self.assertTrue(eh_pastor(self.usuario))

		self._editar()
		self.assertFalse(eh_pastor(self.usuario))

	def test_superusuario_nao_aparece_como_pastor_sem_ser_marcado(self):
		form = UsuarioUpdateForm(instance=self.admin)
		self.assertFalse(form.fields['perfil_pastor'].initial)

	@override_settings(STORAGES=TEST_STORAGES)
	def test_lista_de_usuarios_mostra_quem_e_pastor(self):
		self._editar(perfil_pastor='on')

		resp = self.client.get(reverse('usuarios-lista'))

		pastores = {usuario.username: usuario.eh_pastor for usuario in resp.context['usuarios']}
		self.assertEqual(pastores, {'admin_pastor': False, 'joao': True})


@override_settings(STORAGES=TEST_STORAGES)
class QrCodeDinamicoTests(TestCase):
	def setUp(self):
		self.admin = User.objects.create_superuser(
			username='admin', email='admin@pibvp.com.br', password='segredo123'
		)
		self.staff = User.objects.create_user(
			username='marta', email='marta@pibvp.com.br', password='segredo123', is_staff=True
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

	def test_lista_permite_staff(self):
		self.client.force_login(self.staff)
		response = self.client.get(reverse('qrcodes-lista'))
		self.assertEqual(response.status_code, 200)
		self.assertContains(response, 'Cartaz entrada')

	def test_staff_pode_criar_qrcode(self):
		self.client.force_login(self.staff)
		response = self.client.post(reverse('qrcodes-novo'), data={
			'nome': 'Mural jovens',
			'destino': 'https://example.com/jovens',
			'ativo': 'on',
			'descricao': '',
		})
		novo = QrCodeDinamico.objects.get(nome='Mural jovens')
		self.assertRedirects(response, reverse('qrcodes-detalhe', args=[novo.pk]))
		self.assertEqual(novo.criado_por, self.staff)

	def test_menu_mostra_qrcodes_para_staff_sem_itens_de_superuser(self):
		self.client.force_login(self.staff)
		response = self.client.get(reverse('dashboard'))
		self.assertContains(response, reverse('qrcodes-lista'))
		self.assertNotContains(response, reverse('usuarios-lista'))

	def test_menu_esconde_qrcodes_de_usuario_comum(self):
		self.client.force_login(self.comum)
		response = self.client.get(reverse('dashboard'))
		self.assertNotContains(response, reverse('qrcodes-lista'))

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


@override_settings(STORAGES=TEST_STORAGES)
class TelefoneUsuarioTests(TestCase):
	"""Telefone no cadastro de usuarios e aviso no Painel para quem ainda nao cadastrou."""

	AVISO = 'Seu telefone ainda não está no cadastro.'

	def setUp(self):
		self.usuario = User.objects.create_user('maria_tel', password='x', first_name='Maria')
		self.client.force_login(self.usuario)

	def _salvar_perfil(self, telefone, next_url='/'):
		return self.client.post(reverse('perfil'), {
			'action': 'salvar_perfil',
			'first_name': 'Maria',
			'last_name': '',
			'email': '',
			'telefone': telefone,
			'next': next_url,
		})

	def test_painel_avisa_quem_nao_tem_telefone_com_link_para_o_perfil(self):
		resp = self.client.get(reverse('dashboard'))

		self.assertTrue(resp.context['telefone_pendente'])
		self.assertContains(resp, self.AVISO)
		self.assertContains(resp, f'href="{reverse("perfil")}?next={reverse("dashboard")}"')

	def test_link_do_aviso_abre_o_perfil_no_campo_de_telefone(self):
		resp = self.client.get(reverse('perfil'), {'next': reverse('dashboard')})

		self.assertTrue(resp.context['perfil_form'].fields['telefone'].widget.attrs.get('autofocus'))
		self.assertContains(resp, 'name="next" value="/"')
		self.assertContains(resp, 'id="telefone" class="profile-campo-destaque"')

	def test_cadastrar_telefone_volta_para_o_painel_sem_o_aviso(self):
		resp = self._salvar_perfil('(31) 99999-8888')

		self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
		self.assertEqual(telefone_do_usuario(self.usuario), '(31) 99999-8888')
		resp = self.client.get(reverse('dashboard'))
		self.assertFalse(resp.context['telefone_pendente'])
		self.assertNotContains(resp, self.AVISO)

	def test_telefone_invalido_nao_salva(self):
		resp = self._salvar_perfil('1234')

		self.assertEqual(resp.status_code, 200)
		self.assertIn('telefone', resp.context['perfil_form'].errors)
		self.assertEqual(telefone_do_usuario(self.usuario), '')

	def test_next_de_outro_site_e_ignorado(self):
		resp = self._salvar_perfil('31999998888', next_url='https://exemplo.com/')
		self.assertRedirects(resp, reverse('perfil'), fetch_redirect_response=False)

	def test_admin_cadastra_o_telefone_no_cadastro_de_usuarios(self):
		admin = User.objects.create_superuser('admin_tel', password='x')
		self.client.force_login(admin)

		resp = self.client.post(reverse('usuarios-editar', args=[self.usuario.pk]), {
			'username': 'maria_tel',
			'first_name': 'Maria',
			'last_name': '',
			'email': '',
			'is_active': 'on',
			'telefone': '31 98888-7777',
		})

		self.assertEqual(resp.status_code, 302)
		self.assertEqual(telefone_do_usuario(self.usuario), '31 98888-7777')
		self.assertEqual(UsuarioUpdateForm(instance=self.usuario).fields['telefone'].initial, '31 98888-7777')
		self.assertContains(self.client.get(reverse('usuarios-lista')), '31 98888-7777')

	def test_editar_usuario_nao_troca_quem_esta_logado_no_topo(self):
		admin = User.objects.create_superuser('admin_topo', password='x')
		self.client.force_login(admin)

		resp = self.client.get(reverse('usuarios-editar', args=[self.usuario.pk]))

		self.assertEqual(resp.context['user'], admin)
		self.assertContains(resp, 'Ola, admin_topo')
		self.assertNotContains(resp, 'Ola, maria_tel')

	def test_form_create_salva_o_telefone(self):
		form = UsuarioCreateForm(data={
			'username': 'novo_tel',
			'first_name': '',
			'last_name': '',
			'email': '',
			'is_active': True,
			'is_staff': False,
			'telefone': '(21) 3333-4444',
			'password1': 'SenhaForte1',
			'password2': 'SenhaForte1',
		})
		self.assertTrue(form.is_valid(), form.errors)
		user = form.save()

		self.assertEqual(telefone_do_usuario(user), '(21) 3333-4444')



@override_settings(STORAGES=TEST_STORAGES)
class MarcaDoTopoTests(TestCase):
	def test_marca_do_topo_leva_para_o_painel(self):
		self.client.force_login(User.objects.create_user('marca_topo', password='x'))

		resp = self.client.get(reverse('perfil'))

		self.assertContains(resp, f'<a class="brand-wrap" href="{reverse("dashboard")}" title="Ir para o Painel">')
