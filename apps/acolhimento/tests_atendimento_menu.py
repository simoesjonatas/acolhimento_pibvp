"""Opcoes do bot: ativa (o bot responde) separado de aparece no menu (listada e numerada)."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import atendimento_bot
from apps.acolhimento.forms import OpcaoAtendimentoBotForm
from apps.acolhimento.models import (
	ConfiguracaoAtendimentoBot,
	MensagemContato,
	OpcaoAtendimentoBot,
	PrimeiroContato,
)

SIMPLE_STATIC_STORAGES = {
	'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
	'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class OpcaoForaDoMenuBotTests(TestCase):
	"""As duas opcoes semeadas (migracao 0020) + uma oculta e uma inativa."""

	def setUp(self):
		OpcaoAtendimentoBot.objects.all().delete()
		self.falar = OpcaoAtendimentoBot.objects.create(
			ordem=1, rotulo='Falar com alguem', palavras_chave='atendente, falar',
			resposta='Ja vou chamar alguem.', acao=OpcaoAtendimentoBot.Acao.TRANSFERIR_HUMANO,
		)
		self.oculta = OpcaoAtendimentoBot.objects.create(
			ordem=2, rotulo='Pix da igreja', palavras_chave='pix, oferta, dizimo',
			resposta='Nossa chave pix e ...', aparece_no_menu=False,
		)
		self.inativa = OpcaoAtendimentoBot.objects.create(
			ordem=3, rotulo='Retiro 2025', palavras_chave='retiro',
			resposta='Inscricoes encerradas.', ativa=False,
		)
		self.horario = OpcaoAtendimentoBot.objects.create(
			ordem=4, rotulo='Horario dos cultos', palavras_chave='horario, culto',
			resposta='Domingo as 10h e 18h.',
		)
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.save()

	def _pessoa(self):
		return PrimeiroContato.objects.create(
			nome='Visitante Menu',
			telefone_whatsapp='11970003333',
			como_conheceu=PrimeiroContato.ComoConheceuChoices.OUTRO,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.PARTICIPAR_DE_ALGO,
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
		)

	def _ultima_saida(self, pessoa):
		return MensagemContato.objects.filter(
			pessoa=pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA,
		).order_by('-id').first()

	def test_menu_lista_so_ativas_visiveis_com_numeracao_continua(self):
		self.assertEqual(atendimento_bot.montar_menu(), '1. Falar com alguem\n2. Horario dos cultos')

	def test_numero_segue_a_numeracao_do_menu(self):
		opcoes = atendimento_bot.opcoes_ativas()
		self.assertEqual(atendimento_bot.encontrar_opcao('1', opcoes), self.falar)
		# "2" e o Horario (2o item visivel), nao a opcao oculta que esta na ordem 2.
		self.assertEqual(atendimento_bot.encontrar_opcao('2', opcoes), self.horario)
		self.assertIsNone(atendimento_bot.encontrar_opcao('3', opcoes))

	def test_opcao_fora_do_menu_responde_pela_palavra_chave(self):
		opcoes = atendimento_bot.opcoes_ativas()
		self.assertEqual(atendimento_bot.encontrar_opcao('qual o pix?', opcoes), self.oculta)
		self.assertEqual(atendimento_bot.encontrar_opcao('dizimo', opcoes), self.oculta)

	def test_opcao_inativa_e_ignorada_mesmo_pela_palavra_chave(self):
		opcoes = atendimento_bot.opcoes_ativas()
		self.assertNotIn(self.inativa, opcoes)
		self.assertIsNone(atendimento_bot.encontrar_opcao('retiro', opcoes))

	def test_conversa_completa_com_opcao_oculta(self):
		pessoa = self._pessoa()
		atendimento_bot.processar_entrada(pessoa, 'oi')
		saudacao = self._ultima_saida(pessoa).conteudo
		self.assertIn('1. Falar com alguem', saudacao)
		self.assertIn('2. Horario dos cultos', saudacao)
		self.assertNotIn('Pix', saudacao)
		self.assertNotIn('Retiro', saudacao)

		pessoa.refresh_from_db()
		self.assertTrue(atendimento_bot.processar_entrada(pessoa, 'me passa o pix'))
		self.assertEqual(self._ultima_saida(pessoa).conteudo, 'Nossa chave pix e ...')

	def test_sem_opcoes_no_menu_manda_so_a_saudacao(self):
		OpcaoAtendimentoBot.objects.update(aparece_no_menu=False)
		pessoa = self._pessoa()
		self.assertTrue(atendimento_bot.processar_entrada(pessoa, 'oi'))
		cfg = ConfiguracaoAtendimentoBot.carregar()
		self.assertEqual(self._ultima_saida(pessoa).conteudo, cfg.mensagem_saudacao)


class OpcaoAtendimentoBotFormTests(TestCase):
	def _dados(self, **extra):
		dados = {
			'rotulo': 'Pix', 'palavras_chave': 'pix', 'resposta': 'Chave pix',
			'acao': OpcaoAtendimentoBot.Acao.RESPONDER, 'ativa': 'on', 'aparece_no_menu': 'on',
		}
		dados.update(extra)
		return {chave: valor for chave, valor in dados.items() if valor is not None}

	def test_ativa_fora_do_menu_exige_palavra_chave(self):
		form = OpcaoAtendimentoBotForm(data=self._dados(aparece_no_menu=None, palavras_chave=' , '))
		self.assertFalse(form.is_valid())
		self.assertIn('palavras_chave', form.errors)

	def test_ativa_fora_do_menu_com_palavra_chave_e_valida(self):
		form = OpcaoAtendimentoBotForm(data=self._dados(aparece_no_menu=None))
		self.assertTrue(form.is_valid(), form.errors)
		self.assertFalse(form.cleaned_data['aparece_no_menu'])

	def test_inativa_sem_palavra_chave_e_valida(self):
		form = OpcaoAtendimentoBotForm(data=self._dados(ativa=None, aparece_no_menu=None, palavras_chave=''))
		self.assertTrue(form.is_valid(), form.errors)

	def test_novas_opcoes_aparecem_no_menu_por_padrao(self):
		opcao = OpcaoAtendimentoBot.objects.create(rotulo='X', resposta='Y')
		self.assertTrue(opcao.aparece_no_menu)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class TelaOpcoesAtendimentoTests(TestCase):
	def setUp(self):
		OpcaoAtendimentoBot.objects.all().delete()
		self.visivel = OpcaoAtendimentoBot.objects.create(ordem=1, rotulo='Falar com alguem', resposta='a')
		self.oculta = OpcaoAtendimentoBot.objects.create(
			ordem=2, rotulo='Pix', palavras_chave='pix', resposta='b', aparece_no_menu=False,
		)
		self.inativa = OpcaoAtendimentoBot.objects.create(ordem=3, rotulo='Retiro', resposta='c', ativa=False)
		self.admin = get_user_model().objects.create_superuser('admin_menu', 'admin_menu@ex.com', 'x')
		self.client.force_login(self.admin)

	def test_lista_mostra_numero_real_e_fora_do_menu(self):
		resp = self.client.get(reverse('configuracao-atendimento'))
		self.assertEqual(resp.status_code, 200)
		numeros = {op.pk: op.numero_menu for op in resp.context['opcoes']}
		self.assertEqual(numeros, {self.visivel.pk: 1, self.oculta.pk: None, self.inativa.pk: None})
		self.assertContains(resp, 'fora do menu')
		self.assertEqual(resp.context['total_opcoes_no_menu'], 1)
		self.assertEqual(resp.context['menu_preview'], '1. Falar com alguem')

	def test_aviso_quando_nenhuma_opcao_aparece_no_menu(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.save()
		OpcaoAtendimentoBot.objects.filter(pk=self.visivel.pk).update(aparece_no_menu=False, palavras_chave='falar')
		resp = self.client.get(reverse('configuracao-atendimento'))
		self.assertContains(resp, 'nenhuma aparece no menu')

	def test_editar_tira_do_menu_e_mantem_ativa(self):
		resp = self.client.post(reverse('atendimento-opcao-editar', args=[self.visivel.pk]), {
			'rotulo': 'Falar com alguem', 'palavras_chave': 'atendente', 'resposta': 'a',
			'acao': OpcaoAtendimentoBot.Acao.RESPONDER, 'ativa': 'on',
		})
		self.assertEqual(resp.status_code, 302)
		self.visivel.refresh_from_db()
		self.assertTrue(self.visivel.ativa)
		self.assertFalse(self.visivel.aparece_no_menu)

	def test_formulario_mostra_as_duas_opcoes(self):
		resp = self.client.get(reverse('atendimento-opcao-editar', args=[self.visivel.pk]))
		self.assertContains(resp, 'Opcao ativa (o bot responde a ela)')
		self.assertContains(resp, 'Aparece no menu')
