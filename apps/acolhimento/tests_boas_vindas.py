"""Boas-vindas automaticas: switch no painel e envio logo depois do cadastro."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import boas_vindas
from apps.acolhimento.models import ConfiguracaoProcessamentoFila, MensagemContato, PrimeiroContato
from apps.acolhimento.tests import SIMPLE_STATIC_STORAGES, _dados_form

Status = PrimeiroContato.StatusAcolhimento

META = dict(
	WHATSAPP_PROVIDER='meta',
	META_TEMPLATE_OPT_IN_NOME='boas_vindas_pibvp',
	META_TEMPLATE_OPT_IN_IDIOMA='pt_BR',
	HORAS_ESPERA_OPTIN=0,
	STORAGES=SIMPLE_STATIC_STORAGES,
)


def _ligar(ligado=True):
	cfg = ConfiguracaoProcessamentoFila.carregar()
	cfg.boas_vindas_auto = ligado
	cfg.save()


@override_settings(**META)
class _Base(TestCase):
	def setUp(self):
		cache.clear()
		for alvo in ('obter_template', 'get_phone_number_info'):
			mock = patch(f'apps.acolhimento.meta_service.{alvo}', return_value=None)
			mock.start()
			self.addCleanup(mock.stop)
		self.equipe = get_user_model().objects.create_user('equipe_bv', password='x', is_staff=True)
		self.client.force_login(self.equipe)


class CadastroPelaEquipeTests(_Base):
	def _cadastrar(self, telefone='21988887777'):
		resp = self.client.post(reverse('pessoas-novo'), _dados_form(telefone, nome='Joana'))
		self.assertEqual(resp.status_code, 302)
		return PrimeiroContato.objects.get(nome='Joana Silva')

	def test_desligado_nao_envia(self):
		pessoa = self._cadastrar()
		self.assertFalse(MensagemContato.objects.filter(pessoa=pessoa).exists())
		self.assertEqual(pessoa.status, Status.PRIMEIRO_CONTATO)

	def test_ligado_enfileira_o_template_e_vira_visitante(self):
		_ligar()
		pessoa = self._cadastrar()
		mensagem = MensagemContato.objects.get(pessoa=pessoa)
		self.assertEqual(mensagem.metadata_envio['tipo_template'], 'primeiro_contato_opt_in')
		self.assertEqual(mensagem.metadata_envio['meta_template']['nome'], 'boas_vindas_pibvp')
		self.assertEqual(mensagem.status_fila, MensagemContato.StatusFilaChoices.PENDENTE)
		self.assertEqual(mensagem.criado_por, self.equipe)
		self.assertEqual(pessoa.status, Status.ROBO)

	def test_ligado_mas_sem_template_so_cadastra(self):
		_ligar()
		with override_settings(META_TEMPLATE_OPT_IN_NOME=''):
			pessoa = self._cadastrar()
		self.assertFalse(MensagemContato.objects.filter(pessoa=pessoa).exists())
		self.assertEqual(pessoa.status, Status.PRIMEIRO_CONTATO)

	def test_ligado_respeita_a_carencia(self):
		_ligar()
		with override_settings(HORAS_ESPERA_OPTIN=2):
			pessoa = self._cadastrar()
		self.assertFalse(MensagemContato.objects.filter(pessoa=pessoa).exists())

	def test_falha_ao_enfileirar_nao_derruba_o_cadastro(self):
		_ligar()
		with patch.object(boas_vindas, 'enfileirar', side_effect=RuntimeError('fora do ar')):
			pessoa = self._cadastrar()
		self.assertEqual(pessoa.status, Status.PRIMEIRO_CONTATO)


class AutoCadastroTests(_Base):
	def setUp(self):
		super().setUp()
		self.client.logout()

	def test_ligado_envia_para_quem_se_cadastra_pelo_qr_code(self):
		_ligar()
		resp = self.client.post(reverse('auto-cadastro'), _dados_form('21977776666', nome='Pedro'))
		self.assertEqual(resp.status_code, 302)
		pessoa = PrimeiroContato.objects.get(nome='Pedro Silva')
		mensagem = MensagemContato.objects.get(pessoa=pessoa)
		self.assertIsNone(mensagem.criado_por)
		self.assertEqual(pessoa.status, Status.ROBO)


class AoCadastrarTests(_Base):
	def _pessoa(self, **extra):
		dados = dict(
			nome='Ana', telefone_whatsapp='21966665555', primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM, status=Status.PRIMEIRO_CONTATO,
		)
		dados.update(extra)
		return PrimeiroContato.objects.create(**dados)

	def test_nao_envia_para_quem_ja_escreveu_primeiro(self):
		_ligar()
		self.assertFalse(boas_vindas.ao_cadastrar(self._pessoa(iniciou_interacao=True)))

	def test_nao_envia_sem_whatsapp(self):
		_ligar()
		self.assertFalse(boas_vindas.ao_cadastrar(self._pessoa(telefone_whatsapp='')))

	def test_nao_envia_fora_de_primeiro_contato(self):
		_ligar()
		self.assertFalse(boas_vindas.ao_cadastrar(self._pessoa(status=Status.EM_ACOMPANHAMENTO)))
		self.assertFalse(MensagemContato.objects.exists())


class SwitchNoPainelTests(_Base):
	def test_painel_mostra_o_switch_desligado(self):
		resp = self.client.get(reverse('dashboard'))
		self.assertContains(resp, 'value="boas_vindas_auto"')
		self.assertFalse(resp.context['boas_vindas_auto'])

	def test_liga_e_desliga(self):
		resp = self.client.post(reverse('dashboard'), {'action': 'boas_vindas_auto'})
		self.assertEqual(resp.status_code, 302)
		self.assertTrue(boas_vindas.automatico_ligado())
		self.assertEqual(ConfiguracaoProcessamentoFila.carregar().atualizado_por, self.equipe)
		self.client.post(reverse('dashboard'), {'action': 'boas_vindas_auto'})
		self.assertFalse(boas_vindas.automatico_ligado())

	def test_ligar_nao_dispara_para_quem_ja_estava_esperando(self):
		pessoa = PrimeiroContato.objects.create(
			nome='Esperando', telefone_whatsapp='21955554444', primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		)
		resp = self.client.post(reverse('dashboard'), {'action': 'boas_vindas_auto'}, follow=True)
		self.assertContains(resp, 'Quem ja estava esperando (1 pessoa)')
		self.assertFalse(MensagemContato.objects.filter(pessoa=pessoa).exists())

	def test_so_staff_liga(self):
		comum = get_user_model().objects.create_user('comum_bv', password='x')
		self.client.force_login(comum)
		self.client.post(reverse('dashboard'), {'action': 'boas_vindas_auto'})
		self.assertFalse(boas_vindas.automatico_ligado())

	def test_disparo_manual_continua_funcionando(self):
		pessoa = PrimeiroContato.objects.create(
			nome='Manual', telefone_whatsapp='21944443333', primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		)
		resp = self.client.post(reverse('dashboard'), {'action': 'disparar_template_opt_in'})
		self.assertEqual(resp.status_code, 302)
		self.assertTrue(MensagemContato.objects.filter(pessoa=pessoa).exists())
		pessoa.refresh_from_db()
		self.assertEqual(pessoa.status, Status.ROBO)
