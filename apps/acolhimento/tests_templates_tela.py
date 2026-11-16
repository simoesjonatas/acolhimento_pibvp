"""Tela "Mensagens do WhatsApp": abas, status na Meta e previa do texto."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import templates_tela
from apps.acolhimento.models import TemplateWhatsapp
from apps.acolhimento.tests_meta import META_OK, SIMPLE_STATIC_STORAGES

Tipo = TemplateWhatsapp.Tipo

LISTA = [
	{'name': 'acolhimento_primeiro_contato', 'language': 'pt_BR', 'status': 'APPROVED'},
	{'name': 'gabinete_agendado_pessoa', 'language': 'pt_BR', 'status': 'APPROVED'},
	{'name': 'gabinete_agendado_pastor', 'language': 'en_US', 'status': 'APPROVED'},
	{'name': 'nova_pessoa_atribuida', 'language': 'pt_BR', 'status': 'PENDING'},
]

TEMPLATES = {
	'acolhimento_primeiro_contato': {
		'name': 'acolhimento_primeiro_contato', 'language': 'pt_BR', 'status': 'APPROVED',
		'components': [{'type': 'BODY', 'text': 'Olá, {{1}}! Que alegria ter você na PIBVP.'}],
	},
	'gabinete_agendado_pessoa': {
		'name': 'gabinete_agendado_pessoa', 'language': 'pt_BR', 'status': 'APPROVED',
		'components': [{'type': 'BODY', 'text': 'Olá, {{1}}! Gabinete em {{2}} às {{3}} com {{4}}.'}],
	},
}


def _obter(nome, idioma, timeout=6):
	return TEMPLATES.get(nome, {})


@override_settings(**META_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class _Base(TestCase):
	def setUp(self):
		cache.clear()
		self.addCleanup(cache.clear)
		self.admin = get_user_model().objects.create_superuser('adm_tpl', 'adm_tpl@ex.com', 'x')
		self.client.force_login(self.admin)
		self.url = reverse('configuracao-templates')

	def _salvar(self, tipo, **campos):
		TemplateWhatsapp.objects.update_or_create(tipo=tipo, defaults=campos)

	def _aba_aberta(self, resp):
		html = resp.content.decode()
		return [aba for aba in templates_tela.ABAS_VALIDAS
				if f'id="painel-{aba}" data-painel="{aba}" aria-labelledby="aba-{aba}">' in html]


class AbasTests(_Base):
	def test_abre_na_primeira_aba_e_na_pedida(self):
		self.assertEqual(self._aba_aberta(self.client.get(self.url)), ['conversa'])
		self.assertEqual(self._aba_aberta(self.client.get(self.url, {'aba': 'rodizio'})), ['rodizio'])
		self.assertEqual(self._aba_aberta(self.client.get(self.url, {'aba': 'xpto'})), ['conversa'])

	def test_cada_cartao_na_sua_aba(self):
		html = self.client.get(self.url).content.decode()
		painel_gabinete = html[html.index('id="painel-gabinete"'):html.index('id="painel-rodizio"')]
		self.assertIn('data-cartao="gab_pessoa"', painel_gabinete)
		self.assertIn('data-cartao="gab_pastor"', painel_gabinete)
		self.assertNotIn('data-cartao="opt_in"', painel_gabinete)

	def _dados(self, **extra):
		resp = self.client.get(self.url)
		dados = {campo.html_name: campo.value() or '' for campo in resp.context['form']}
		dados.update(extra)
		return dados

	def test_salvar_volta_para_a_mesma_aba(self):
		resp = self.client.post(self.url, self._dados(aba='gabinete', gab_pessoa_meta_nome='gabinete_agendado_pessoa'))
		self.assertEqual(resp['Location'], self.url + '?aba=gabinete')

	def test_erro_abre_a_aba_do_campo(self):
		resp = self.client.post(self.url, self._dados(aba='conversa', resp_meta_nome='Nome Errado'))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(self._aba_aberta(resp), ['rodizio'])
		self.assertContains(resp, 'tpl-aba-erro')


class StatusTests(_Base):
	def _status(self):
		with patch('apps.acolhimento.meta_service.list_templates', return_value=LISTA), \
				patch('apps.acolhimento.meta_service.obter_template', side_effect=_obter):
			resp = self.client.get(reverse('configuracao-templates-status'))
		self.assertEqual(resp.status_code, 200)
		return resp.json()

	def test_aprovado_mostra_a_previa_com_dados_de_exemplo(self):
		self._salvar(Tipo.PRIMEIRO_CONTATO, meta_nome='acolhimento_primeiro_contato', meta_idioma='pt_BR')
		self._salvar(Tipo.GABINETE_PESSOA, meta_nome='gabinete_agendado_pessoa', meta_idioma='pt_BR')
		dados = self._status()
		opt_in = dados['cartoes']['opt_in']
		self.assertEqual((opt_in['rotulo'], opt_in['classe']), ('Aprovado na Meta', 'ok'))
		self.assertEqual(opt_in['previa'], 'Olá, Maria! Que alegria ter você na PIBVP.')
		self.assertEqual(
			dados['cartoes']['gab_pessoa']['previa'], 'Olá, Maria! Gabinete em 20/11/2026 às 19:30 com Pr. João.',
		)
		self.assertEqual(dados['aprovados'], [
			'acolhimento_primeiro_contato', 'gabinete_agendado_pastor', 'gabinete_agendado_pessoa',
		])

	def test_nao_encontrado_e_idioma_errado(self):
		self._salvar(Tipo.CONTINUAR, meta_nome='nao_existe', meta_idioma='pt_BR')
		self._salvar(Tipo.GABINETE_PASTOR, meta_nome='gabinete_agendado_pastor', meta_idioma='pt_BR')
		cartoes = self._status()['cartoes']
		self.assertEqual(cartoes['continuar']['rotulo'], 'Não encontrado na Meta')
		self.assertIn('exatamente igual', cartoes['continuar']['aviso'])
		self.assertEqual(cartoes['gab_pastor']['rotulo'], 'Não encontrado na Meta')
		self.assertIn('outro idioma: en_US', cartoes['gab_pastor']['aviso'])

	def test_em_analise_e_sem_template(self):
		self._salvar(Tipo.RESPONSAVEL_ATRIBUIDO, meta_nome='nova_pessoa_atribuida', meta_idioma='pt_BR')
		cartoes = self._status()['cartoes']
		self.assertEqual((cartoes['resp']['rotulo'], cartoes['resp']['classe']), ('Em análise na Meta', 'espera'))
		self.assertEqual(cartoes['gab_pessoa']['rotulo'], 'Sem template')

	def test_avisa_quando_as_variaveis_nao_batem(self):
		self._salvar(
			Tipo.GABINETE_PESSOA, meta_nome='gabinete_agendado_pessoa', meta_idioma='pt_BR',
			content_variables='{"1": "{nome}", "2": "{data}"}',
		)
		aviso = self._status()['cartoes']['gab_pessoa']['aviso']
		self.assertIn('O template tem 4 variável(is) e o sistema manda 2', aviso)

	def test_meta_fora_do_ar(self):
		self._salvar(Tipo.PRIMEIRO_CONTATO, meta_nome='acolhimento_primeiro_contato')
		with patch('apps.acolhimento.meta_service.list_templates', return_value=[]):
			cartoes = self.client.get(reverse('configuracao-templates-status')).json()['cartoes']
		self.assertEqual(cartoes['opt_in']['rotulo'], 'Não deu para consultar a Meta')

	def test_fora_do_modo_meta_nao_consulta(self):
		with override_settings(WHATSAPP_PROVIDER='evolution'), \
				patch('apps.acolhimento.meta_service.list_templates') as lista:
			dados = self.client.get(reverse('configuracao-templates-status')).json()
		lista.assert_not_called()
		self.assertEqual(dados, {'cartoes': {}, 'aprovados': []})

	def test_so_superusuario(self):
		staff = get_user_model().objects.create_user('staff_tpl', password='x', is_staff=True)
		self.client.force_login(staff)
		self.assertEqual(self.client.get(reverse('configuracao-templates-status')).status_code, 403)
