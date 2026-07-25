from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.acolhimento.fila_processor import processar_fila_mensagens
from apps.acolhimento.forms import AutoCadastroPrimeiroContatoForm, PrimeiroContatoForm
from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato
from apps.acolhimento.views import (
	PERMISSAO_CONVERSAR_PESSOAS,
	DisparoMensagemMassaView,
	MensagemFilaListView,
	PrimeiroContatoMensagensView,
	ProcessamentoFilaControleView,
)
from apps.core.views import UsuarioListView


def _dados_form(telefone, nome='Fulano'):
	return {
		'nome': nome,
		'telefone_whatsapp': telefone,
		'primeira_vez': 'True',
		'como_conheceu': PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		'o_que_busca': PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
	}


class TelefoneWhatsappUnicoTests(TestCase):
	def setUp(self):
		self.pessoa = PrimeiroContato.objects.create(
			nome='Maria',
			telefone_whatsapp='31999999999',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
		)

	def test_bloqueia_numero_identico(self):
		form = PrimeiroContatoForm(data=_dados_form('31999999999'))
		self.assertFalse(form.is_valid())
		self.assertIn('telefone_whatsapp', form.errors)

	def test_bloqueia_numero_com_mascara_diferente(self):
		form = PrimeiroContatoForm(data=_dados_form('(31) 99999-9999'))
		self.assertFalse(form.is_valid())
		self.assertIn('telefone_whatsapp', form.errors)

	def test_bloqueia_numero_com_ddi_55(self):
		form = PrimeiroContatoForm(data=_dados_form('5531999999999'))
		self.assertFalse(form.is_valid())
		self.assertIn('telefone_whatsapp', form.errors)

	def test_bloqueia_numero_sem_nono_digito(self):
		form = PrimeiroContatoForm(data=_dados_form('3199999999'))
		self.assertFalse(form.is_valid())
		self.assertIn('telefone_whatsapp', form.errors)

	def test_permite_numero_diferente(self):
		form = PrimeiroContatoForm(data=_dados_form('31988888888'))
		self.assertTrue(form.is_valid(), form.errors)

	def test_permite_editar_a_propria_pessoa(self):
		form = PrimeiroContatoForm(data=_dados_form('31999999999', nome='Maria Atualizada'), instance=self.pessoa)
		self.assertTrue(form.is_valid(), form.errors)

	def test_team_form_revela_nome_do_duplicado(self):
		form = PrimeiroContatoForm(data=_dados_form('31999999999'))
		self.assertFalse(form.is_valid())
		self.assertIn('Maria', form.errors['telefone_whatsapp'][0])

	def test_auto_cadastro_nao_revela_nome_do_duplicado(self):
		form = AutoCadastroPrimeiroContatoForm(data=_dados_form('31999999999'))
		self.assertFalse(form.is_valid())
		self.assertNotIn('Maria', form.errors['telefone_whatsapp'][0])


class StatusEvolutivoTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user(username='equipe', password='x', is_staff=True)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Joao',
			telefone_whatsapp='31977777777',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO,
		)
		self.client.force_login(self.user)

	def test_participante_vem_antes_de_membro(self):
		valores = list(PrimeiroContato.StatusAcolhimento.values)
		self.assertIn('participante', valores)
		self.assertLess(valores.index('participante'), valores.index('membro'))

	def test_troca_status_valida_e_registra_timeline(self):
		url = reverse('pessoas-status', args=[self.pessoa.pk])
		resp = self.client.post(url, {'status': 'participante'})
		# fetch_redirect_response=False evita renderizar o alvo (incompat Django 4.2 + Python 3.14).
		self.assertRedirects(
			resp,
			reverse('pessoas-detalhe', args=[self.pessoa.pk]),
			fetch_redirect_response=False,
		)
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.PARTICIPANTE)
		self.assertTrue(
			self.pessoa.interacoes.filter(tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO).exists()
		)

	def test_troca_status_invalida_nao_altera(self):
		url = reverse('pessoas-status', args=[self.pessoa.pk])
		self.client.post(url, {'status': 'inexistente'})
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)


class BloqueioWhatsappOptInTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user(username='equipe', password='x', is_staff=True)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Visitante',
			telefone_whatsapp='31955554444',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.ROBO,
			iniciou_interacao=False,
		)
		self.client.force_login(self.user)

	def test_bloqueia_mensagem_whatsapp_manual_sem_resposta_ao_template(self):
		url = reverse('pessoas-enfileirar-mensagem', args=[self.pessoa.pk])
		resp = self.client.post(
			url,
			{
				'canal': MensagemContato.CanalChoices.WHATSAPP,
				'conteudo': 'Oi, tudo bem?',
			},
		)

		self.assertEqual(resp.status_code, 302)
		self.assertFalse(
			MensagemContato.objects.filter(
				pessoa=self.pessoa,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
			).exists()
		)

	def test_permite_email_manual_sem_resposta_ao_template(self):
		url = reverse('pessoas-enfileirar-mensagem', args=[self.pessoa.pk])
		resp = self.client.post(
			url,
			{
				'canal': MensagemContato.CanalChoices.EMAIL,
				'conteudo': 'Oi por email.',
			},
		)

		self.assertEqual(resp.status_code, 302)
		self.assertTrue(
			MensagemContato.objects.filter(
				pessoa=self.pessoa,
				canal=MensagemContato.CanalChoices.EMAIL,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
			).exists()
		)

	def test_processador_cancela_whatsapp_sem_resposta_ao_template(self):
		mensagem = MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Mensagem normal antes do aceite.',
		)

		resultado = processar_fila_mensagens(limit=5)
		mensagem.refresh_from_db()

		self.assertEqual(resultado['total_processado'], 1)
		self.assertEqual(resultado['falha'], 1)
		self.assertEqual(mensagem.status_fila, MensagemContato.StatusFilaChoices.CANCELADA)
		self.assertIn('ainda nao respondeu', mensagem.erro_ultimo_envio)
		self.assertEqual(mensagem.metadata_envio['bloqueio_envio']['motivo'], 'whatsapp_sem_interacao_previa')

	def test_processador_permite_template_primeiro_contato_sem_resposta(self):
		mensagem = MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Template opt-in enfileirado',
			metadata_envio={'tipo_template': 'primeiro_contato_opt_in'},
		)

		resultado = processar_fila_mensagens(limit=5, dry_run=True)
		mensagem.refresh_from_db()

		self.assertEqual(resultado['total_processado'], 1)
		self.assertEqual(resultado['falha'], 0)
		self.assertEqual(mensagem.status_fila, MensagemContato.StatusFilaChoices.PENDENTE)

	def test_webhook_resposta_libera_envio_whatsapp_para_pessoa_existente(self):
		resp = self.client.post(
			reverse('mensagens-webhook-twilio-inbound'),
			{
				'From': 'whatsapp:+5531955554444',
				'MessageSid': 'SM123',
				'Body': 'Pode me mandar mensagem.',
			},
		)

		self.assertEqual(resp.status_code, 200)
		self.pessoa.refresh_from_db()
		self.assertTrue(self.pessoa.iniciou_interacao)
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		self.assertTrue(
			self.pessoa.interacoes.filter(
				tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
				descricao__icontains='liberou',
			).exists()
		)


class PermissaoConversasPessoasTests(TestCase):
	def setUp(self):
		self.factory = RequestFactory()
		self.user = get_user_model().objects.create_user(username='mensagens', password='x', is_staff=False)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Visitante',
			telefone_whatsapp='31966666666',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INDICACAO,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.FAZER_AMIZADES,
		)

	def test_usuario_com_permissao_acessa_conversa_individual(self):
		permission = Permission.objects.get(codename='pode_conversar_pessoas')
		self.user.user_permissions.add(permission)
		request = self.factory.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		request.user = self.user

		resp = PrimeiroContatoMensagensView.as_view()(request, pk=self.pessoa.pk)

		self.assertEqual(resp.status_code, 200)
		self.assertTrue(self.user.has_perm(PERMISSAO_CONVERSAR_PESSOAS))

	def test_usuario_com_permissao_nao_acessa_telas_administrativas_de_mensagem(self):
		permission = Permission.objects.get(codename='pode_conversar_pessoas')
		self.user.user_permissions.add(permission)
		views_bloqueadas = [
			(reverse('mensagens-fila'), MensagemFilaListView.as_view()),
			(reverse('mensagens-processamento'), ProcessamentoFilaControleView.as_view()),
			(reverse('mensagens-disparo-massa'), DisparoMensagemMassaView.as_view()),
			(reverse('usuarios-lista'), UsuarioListView.as_view()),
		]

		for url, view in views_bloqueadas:
			with self.subTest(url=url):
				request = self.factory.get(url)
				request.user = self.user
				with self.assertRaises(PermissionDenied):
					view(request)

	def test_usuario_sem_permissao_nao_acessa_conversa_individual(self):
		request = self.factory.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		request.user = self.user

		with self.assertRaises(PermissionDenied):
			PrimeiroContatoMensagensView.as_view()(request, pk=self.pessoa.pk)
