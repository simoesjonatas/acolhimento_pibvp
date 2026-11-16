"""Usuario comum ve so os proprios acolhidos; admin e pastor veem a base toda."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import visibilidade
from apps.acolhimento.models import (
	ConviteQuestionario,
	InteracaoAcolhimento,
	MensagemContato,
	PrimeiroContato,
	Questionario,
)
from apps.acolhimento.tests import SIMPLE_STATIC_STORAGES, _dados_form
from apps.acolhimento.tests_midia import _PastaMidiaTemporaria


def _usuario(nome, *perms, **extra):
	user = get_user_model().objects.create_user(nome, password='x', **extra)
	for codename in perms:
		user.user_permissions.add(Permission.objects.get(codename=codename))
	return get_user_model().objects.get(pk=user.pk)


def _pessoa(nome, telefone, responsavel=None):
	return PrimeiroContato.objects.create(
		nome=nome, telefone_whatsapp=telefone, primeira_vez=True,
		como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO,
		responsavel_atual=responsavel,
	)


def _entrada(pessoa, conteudo):
	return MensagemContato.objects.create(
		pessoa=pessoa, canal=MensagemContato.CanalChoices.WHATSAPP,
		direcao=MensagemContato.DirecaoChoices.ENTRADA,
		status_fila=MensagemContato.StatusFilaChoices.ENVIADA, conteudo=conteudo,
	)


class QuemVeTudoTests(TestCase):
	def test_regra(self):
		self.assertTrue(visibilidade.ve_toda_base(_usuario('adm', is_staff=True)))
		self.assertTrue(visibilidade.ve_toda_base(_usuario('sup', is_superuser=True)))
		self.assertTrue(visibilidade.ve_toda_base(_usuario('pastor', 'pode_ver_gabinete')))
		self.assertFalse(visibilidade.ve_toda_base(_usuario('vol', 'pode_conversar_pessoas')))
		self.assertFalse(visibilidade.ve_toda_base(_usuario('sem_perm')))


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class VoluntarioTests(_PastaMidiaTemporaria, TestCase):
	def setUp(self):
		super().setUp()
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.vol = _usuario('vol', 'pode_conversar_pessoas', first_name='Vera')
		self.outro = _usuario('outro', 'pode_conversar_pessoas', first_name='Otto')
		self.minha = _pessoa('Minha Pessoa', '21911112222', self.vol)
		self.alheia = _pessoa('Pessoa Alheia', '21933334444', self.outro)
		self.sem_dono = _pessoa('Pessoa Sem Dono', '21955556666')
		_entrada(self.minha, 'oi da minha')
		self.msg_alheia = _entrada(self.alheia, 'segredo da alheia')
		InteracaoAcolhimento.objects.create(
			pessoa=self.alheia, tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO, descricao='passo da alheia',
		)
		self.client.force_login(self.vol)

	def test_lista_so_mostra_os_acolhidos_dela(self):
		resp = self.client.get(reverse('pessoas-lista'))
		self.assertEqual([p.nome for p in resp.context['pessoas']], ['Minha Pessoa'])
		self.assertContains(resp, 'só as pessoas com você como responsável')
		self.assertNotContains(resp, 'Somente meus contatos')
		# Busca tambem nao acha quem e de outro voluntario.
		resp = self.client.get(reverse('pessoas-lista'), {'q': 'Alheia'})
		self.assertEqual(resp.context['total_filtrado'], 0)

	def test_telas_de_pessoa_alheia_respondem_404(self):
		pk = self.alheia.pk
		gets = [
			reverse('pessoas-detalhe', args=[pk]),
			reverse('pessoas-editar', args=[pk]),
			reverse('pessoas-mensagens', args=[pk]),
			reverse('pessoas-mensagens-carregar', args=[pk]),
			reverse('pessoas-mensagens-novas', args=[pk]),
		]
		for url in gets:
			with self.subTest(url=url):
				self.assertEqual(self.client.get(url).status_code, 404)
		posts = [
			(reverse('pessoas-status', args=[pk]), {'status': 'membro'}),
			(reverse('pessoas-enfileirar-mensagem', args=[pk]), {'conteudo': 'oi'}),
			(reverse('pessoas-template-continuar', args=[pk]), {}),
			(reverse('pessoas-gerar-convite', args=[pk]), {'questionario': 1}),
			(reverse('mensagens-excluir', args=[self.msg_alheia.pk]), {}),
		]
		for url, dados in posts:
			with self.subTest(url=url):
				self.assertEqual(self.client.post(url, dados).status_code, 404)
		self.alheia.refresh_from_db()
		self.assertEqual(self.alheia.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		self.assertTrue(MensagemContato.objects.filter(pk=self.msg_alheia.pk).exists())

	def test_sem_dono_tambem_fica_de_fora(self):
		self.assertEqual(self.client.get(reverse('pessoas-detalhe', args=[self.sem_dono.pk])).status_code, 404)

	def test_a_propria_pessoa_abre_normalmente(self):
		self.assertEqual(self.client.get(reverse('pessoas-detalhe', args=[self.minha.pk])).status_code, 200)
		self.assertContains(self.client.get(reverse('pessoas-mensagens', args=[self.minha.pk])), 'oi da minha')

	def test_midia_de_pessoa_alheia_404(self):
		self.msg_alheia.midia_status = MensagemContato.MidiaStatusChoices.BAIXADA
		self.msg_alheia.midia_mime = 'audio/ogg'
		self.msg_alheia.midia_tamanho = 3
		self.msg_alheia.midia_arquivo.save('a.ogg', ContentFile(b'abc'), save=True)
		self.assertEqual(self.client.get(reverse('mensagens-midia', args=[self.msg_alheia.pk])).status_code, 404)

	def test_aguardando_resposta_so_os_dela(self):
		resp = self.client.get(reverse('aguardando-resposta'))
		self.assertEqual([p.nome for p in resp.context['pessoas']], ['Minha Pessoa'])
		self.assertEqual(resp.context['resumo']['total'], 1)
		self.assertNotContains(resp, 'Pessoa Alheia')
		self.assertNotContains(resp, 'sem responsável')

	def test_respostas_de_questionario_so_os_dela(self):
		questionario = Questionario.objects.create(titulo='Q', ativo=True)
		minha = ConviteQuestionario.objects.create(questionario=questionario, pessoa=self.minha)
		alheia = ConviteQuestionario.objects.create(questionario=questionario, pessoa=self.alheia)
		resp = self.client.get(reverse('questionarios-respostas'))
		self.assertEqual([c.pk for c in resp.context['convites']], [minha.pk])
		self.assertEqual(resp.context['resumo_convites']['total'], 1)
		self.assertEqual(self.client.get(reverse('convite-resposta', args=[alheia.pk])).status_code, 404)
		self.assertEqual(self.client.post(reverse('convite-enviar-whatsapp', args=[alheia.pk])).status_code, 404)

	def test_exportar_csv_so_os_dela(self):
		conteudo = self.client.get(reverse('pessoas-exportar-csv')).content.decode()
		self.assertIn('Minha Pessoa', conteudo)
		self.assertNotIn('Pessoa Alheia', conteudo)

	def test_painel_conta_e_lista_so_os_dela(self):
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.context['total_pessoas'], 1)
		self.assertEqual(resp.context['total_acompanhamento'], 1)
		self.assertNotContains(resp, 'passo da alheia')
		self.assertNotContains(resp, 'id="precisam-de-atencao"')
		self.assertContains(resp, 'id="meus-acolhidos"')

	def test_quem_ela_cadastra_vira_acolhido_dela(self):
		resp = self.client.post(reverse('pessoas-novo'), _dados_form('21977778888', nome='Nova'))
		self.assertEqual(resp.status_code, 302)
		nova = PrimeiroContato.objects.get(nome='Nova Silva')
		self.assertEqual(nova.responsavel_atual, self.vol)
		self.assertEqual(self.client.get(reverse('pessoas-detalhe', args=[nova.pk])).status_code, 200)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class AdminEPastorVeemTudoTests(TestCase):
	def setUp(self):
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.vol = _usuario('vol2', 'pode_conversar_pessoas')
		_pessoa('Do Voluntario', '21911110000', self.vol)
		_pessoa('Sem Dono', '21922220000')

	def test_admin_e_pastor(self):
		for usuario in (_usuario('adm2', is_staff=True), _usuario('pastor2', 'pode_ver_gabinete')):
			with self.subTest(usuario=usuario.username):
				self.client.force_login(usuario)
				resp = self.client.get(reverse('pessoas-lista'))
				self.assertEqual(resp.context['total_filtrado'], 2)
				self.assertContains(self.client.get(reverse('dashboard')), 'id="precisam-de-atencao"')
