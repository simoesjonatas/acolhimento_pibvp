"""Testes do relatorio de custo do WhatsApp (API oficial da Meta). Sem rede."""
import hashlib
import hmac
import importlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import custo_whatsapp, meta_service
from apps.acolhimento.models import MensagemContato, PrimeiroContato, TabelaPrecoWhatsapp

SIMPLE_STATIC_STORAGES = {
	'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
	'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

META = dict(
	WHATSAPP_PROVIDER='meta',
	META_WA_ACCESS_TOKEN='token-teste',
	META_WA_PHONE_NUMBER_ID='1234567890',
	META_WA_BUSINESS_ACCOUNT_ID='9876543210',
	META_WA_APP_SECRET='app-secret-teste',
	META_WA_VERIFY_TOKEN='verify-teste',
)


def _pessoa(nome='Maria Teste', telefone='21971347985'):
	return PrimeiroContato.objects.create(
		nome=nome,
		telefone_whatsapp=telefone,
		primeira_vez=True,
		como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
	)


def _enviada(pessoa, *, quando=None, categoria='', tipo='', metadata=None, status=None, wamid=''):
	metadata = {'meta': {'sid': wamid or 'x'}} if metadata is None else metadata
	return MensagemContato.objects.create(
		pessoa=pessoa,
		canal=MensagemContato.CanalChoices.WHATSAPP,
		direcao=MensagemContato.DirecaoChoices.SAIDA,
		status_fila=status or MensagemContato.StatusFilaChoices.ENVIADA,
		conteudo='ola',
		enviada_em=quando or timezone.now(),
		referencia_externa=wamid,
		metadata_envio=metadata,
		cobranca_categoria=categoria,
		cobranca_tipo=tipo,
	)


def _precos(**valores):
	tabela = TabelaPrecoWhatsapp.carregar()
	for campo, valor in valores.items():
		setattr(tabela, campo, Decimal(valor))
	tabela.save()
	return tabela


# ---------------------------------------------------------------------------
# Gravacao pelo webhook
# ---------------------------------------------------------------------------
@override_settings(**META, DEBUG=False)
class CobrancaWebhookTests(TestCase):
	def _postar_status(self, wamid, status, pricing=None, errors=None):
		item = {'id': wamid, 'status': status, 'timestamp': '1700000000', 'recipient_id': '5521971347985'}
		if pricing:
			item['pricing'] = pricing
		if errors:
			item['errors'] = errors
		payload = {
			'object': 'whatsapp_business_account',
			'entry': [{'id': '1', 'changes': [{'field': 'messages', 'value': {'statuses': [item]}}]}],
		}
		corpo = json.dumps(payload).encode('utf-8')
		assinatura = 'sha256=' + hmac.new(b'app-secret-teste', corpo, hashlib.sha256).hexdigest()
		return self.client.post(
			reverse('mensagens-webhook-meta'), data=corpo, content_type='application/json',
			HTTP_X_HUB_SIGNATURE_256=assinatura,
		)

	def test_status_com_pricing_grava_categoria_e_tipo(self):
		msg = _enviada(_pessoa(), wamid='wamid.C1')
		resp = self._postar_status('wamid.C1', 'sent', pricing={
			'billable': True, 'pricing_model': 'PMP', 'type': 'regular', 'category': 'marketing',
		})
		self.assertEqual(resp.status_code, 200)
		msg.refresh_from_db()
		self.assertEqual(msg.cobranca_categoria, 'marketing')
		self.assertEqual(msg.cobranca_tipo, 'regular')
		self.assertEqual(msg.cobranca_modelo, 'PMP')
		self.assertIsNotNone(msg.cobranca_registrada_em)
		self.assertTrue(msg.cobravel)

	def test_status_gratuito(self):
		msg = _enviada(_pessoa(), wamid='wamid.C2')
		self._postar_status('wamid.C2', 'delivered', pricing={
			'pricing_model': 'PMP', 'type': 'free_customer_service', 'category': 'service',
		})
		msg.refresh_from_db()
		self.assertEqual(msg.cobranca_categoria, 'service')
		self.assertFalse(msg.cobravel)

	def test_status_sem_pricing_nao_apaga_o_que_ja_tinha(self):
		msg = _enviada(_pessoa(), wamid='wamid.C3', categoria='utility', tipo='regular')
		self._postar_status('wamid.C3', 'read')
		msg.refresh_from_db()
		self.assertEqual(msg.cobranca_categoria, 'utility')
		self.assertIsNotNone(msg.lida_em)

	def test_extrair_cobranca_de_webhook_antigo_so_com_billable(self):
		self.assertEqual(
			custo_whatsapp.extrair_cobranca({'pricing': {'billable': False, 'pricing_model': 'CBP', 'category': 'service'}}),
			{'categoria': 'service', 'tipo': 'free_customer_service', 'modelo': 'CBP'},
		)
		self.assertIsNone(custo_whatsapp.extrair_cobranca({'id': 'x'}))


# ---------------------------------------------------------------------------
# Classificacao e somas
# ---------------------------------------------------------------------------
class ResumoCustoTests(TestCase):
	def setUp(self):
		cache.clear()
		self.pessoa = _pessoa()
		self.hoje = timezone.localdate()

	def test_origem_da_mensagem(self):
		self.assertEqual(custo_whatsapp.origem_da_mensagem({'tipo_template': 'primeiro_contato_opt_in'}), 'primeiro_contato')
		self.assertEqual(custo_whatsapp.origem_da_mensagem({'tipo_template': 'continuar_conversa'}), 'continuar')
		self.assertEqual(custo_whatsapp.origem_da_mensagem({'tipo_template': 'marketing_massa'}), 'marketing')
		self.assertEqual(custo_whatsapp.origem_da_mensagem({'origem': 'bot_atendimento'}), 'bot')
		self.assertEqual(custo_whatsapp.origem_da_mensagem({'origem': 'disparo_massa'}), 'disparo_livre')
		self.assertEqual(custo_whatsapp.origem_da_mensagem({}), 'equipe')

	def test_categorias_derivadas_usam_preco_base(self):
		tabela = _precos(preco_marketing='0.35', preco_authentication='0.05')
		self.assertEqual(custo_whatsapp.preco_unitario(tabela, 'marketing_lite'), Decimal('0.35'))
		self.assertEqual(custo_whatsapp.preco_unitario(tabela, 'authentication-international'), Decimal('0.05'))
		self.assertEqual(custo_whatsapp.preco_unitario(tabela, 'referral_conversion'), Decimal('0'))

	def test_resumo_soma_por_categoria_origem_e_dia(self):
		tabela = _precos(preco_marketing='0.35', preco_utility='0.04', preco_service='0.02')
		_enviada(self.pessoa, categoria='marketing', tipo='regular',
			metadata={'meta': {}, 'tipo_template': 'marketing_massa'})
		_enviada(self.pessoa, categoria='marketing', tipo='regular',
			metadata={'meta': {}, 'tipo_template': 'marketing_massa'})
		_enviada(self.pessoa, categoria='utility', tipo='regular',
			metadata={'meta': {}, 'tipo_template': 'primeiro_contato_opt_in'})
		_enviada(self.pessoa, categoria='service', tipo='free_customer_service',
			metadata={'meta': {}, 'origem': 'bot_atendimento'})
		_enviada(self.pessoa, metadata={'meta': {}})  # ainda sem confirmacao
		_enviada(self.pessoa, status=MensagemContato.StatusFilaChoices.FALHA, metadata={'meta': {}})

		dados = custo_whatsapp.resumo(*custo_whatsapp.periodo_padrao(self.hoje), tabela=tabela)

		total = dados['total']
		self.assertEqual(total.total, 6)
		self.assertEqual(total.cobradas, 3)
		self.assertEqual(total.gratuitas, 1)
		self.assertEqual(total.sem_confirmacao, 1)
		self.assertEqual(total.falhas, 1)
		self.assertEqual(total.custo_estimado, Decimal('0.74'))

		categorias = {linha.chave: linha for linha in dados['por_categoria']}
		self.assertEqual(categorias['marketing'].cobradas, 2)
		self.assertEqual(categorias['marketing'].custo_estimado, Decimal('0.70'))
		self.assertEqual(categorias['service'].gratuitas, 1)
		self.assertNotIn('authentication', categorias)

		origens = {linha.chave: linha for linha in dados['por_origem']}
		self.assertEqual(origens['marketing'].total, 2)
		self.assertEqual(origens['primeiro_contato'].custo_estimado, Decimal('0.04'))
		self.assertEqual(origens['bot'].gratuitas, 1)
		self.assertEqual(len(dados['por_dia']), 1)

	def test_periodo_exclui_fora_e_mensagens_de_outros_providers(self):
		inicio, fim = custo_whatsapp.periodo_padrao(self.hoje)
		fora = timezone.make_aware(datetime.combine(inicio - timedelta(days=1), datetime.min.time())) + timedelta(hours=12)
		_enviada(self.pessoa, quando=fora, categoria='utility', tipo='regular')
		_enviada(self.pessoa, metadata={'evolution': {'sid': 'x'}})  # Evolution: nao entra
		_enviada(self.pessoa, metadata={'twilio': {'sid': 'x'}})  # Twilio: nao entra
		dentro = _enviada(self.pessoa, categoria='utility', tipo='regular')

		ids = list(custo_whatsapp.mensagens_do_periodo(inicio, fim).values_list('id', flat=True))
		self.assertEqual(ids, [dentro.id])

	def test_periodo_mes_anterior(self):
		self.assertEqual(
			custo_whatsapp.periodo_mes_anterior(date(2026, 3, 15)),
			(date(2026, 2, 1), date(2026, 2, 28)),
		)
		self.assertEqual(
			custo_whatsapp.periodo_mes_anterior(date(2026, 1, 5)),
			(date(2025, 12, 1), date(2025, 12, 31)),
		)


# ---------------------------------------------------------------------------
# Custo oficial
# ---------------------------------------------------------------------------
@override_settings(**META)
class CustoOficialTests(TestCase):
	def setUp(self):
		cache.clear()

	def test_agrega_data_points_por_categoria(self):
		pontos = [
			{'start': 1, 'end': 2, 'pricing_category': 'MARKETING', 'pricing_type': 'REGULAR', 'volume': 10, 'cost': 3.5},
			{'start': 2, 'end': 3, 'pricing_category': 'MARKETING', 'pricing_type': 'REGULAR', 'volume': 2, 'cost': 0.7},
			{'start': 1, 'end': 2, 'pricing_category': 'SERVICE', 'pricing_type': 'FREE_CUSTOMER_SERVICE', 'volume': 40, 'cost': 0},
		]
		with patch('apps.acolhimento.meta_service.get_pricing_analytics', return_value=pontos) as consulta:
			resultado = custo_whatsapp.custo_oficial(date(2026, 10, 1), date(2026, 10, 31))
			# Segunda chamada vem do cache.
			custo_whatsapp.custo_oficial(date(2026, 10, 1), date(2026, 10, 31))

		consulta.assert_called_once()
		self.assertEqual(resultado['custo'], Decimal('4.2'))
		self.assertEqual(resultado['volume'], 52)
		self.assertEqual(resultado['por_categoria']['marketing']['custo'], Decimal('4.2'))
		self.assertEqual(resultado['por_categoria']['service']['volume'], 40)

	def test_indisponivel_devolve_none(self):
		with patch('apps.acolhimento.meta_service.get_pricing_analytics', return_value=None):
			self.assertIsNone(custo_whatsapp.custo_oficial(date(2026, 10, 1), date(2026, 10, 31)))

	def test_get_pricing_analytics_monta_consulta_e_le_resposta(self):
		capturado = {}

		class _Resp:
			status = 200

			def read(self):
				return json.dumps({'pricing_analytics': {'data': [{'data_points': [
					{'start': 1, 'end': 2, 'pricing_category': 'UTILITY', 'volume': 3, 'cost': 0.12},
				]}]}, 'id': '9876543210'}).encode()

			def __enter__(self):
				return self

			def __exit__(self, *args):
				return False

		def fake_urlopen(req, timeout=None):
			capturado['url'] = req.full_url
			return _Resp()

		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', side_effect=fake_urlopen):
			pontos = meta_service.get_pricing_analytics(1000, 2000)

		self.assertIn('/9876543210?', capturado['url'])
		self.assertIn('pricing_analytics.start%281000%29.end%282000%29', capturado['url'])
		self.assertIn('granularity%28DAILY%29', capturado['url'])
		self.assertEqual(pontos[0]['pricing_category'], 'UTILITY')

	@override_settings(META_WA_BUSINESS_ACCOUNT_ID='')
	def test_sem_waba_nao_consulta(self):
		with patch('apps.acolhimento.meta_service.urllib.request.urlopen') as urlopen:
			self.assertIsNone(meta_service.get_pricing_analytics(1, 2))
		urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Preenchimento retroativo (migracao 0028)
# ---------------------------------------------------------------------------
class BackfillCobrancaTests(TestCase):
	def test_recupera_cobranca_do_historico_do_webhook(self):
		migracao = importlib.import_module('apps.acolhimento.migrations.0028_custo_whatsapp')
		pessoa = _pessoa()
		com_pricing = _enviada(pessoa, metadata={'meta': {}, 'meta_webhook': [
			{'received_at': '2026-10-31T12:00:00+00:00', 'status': 'sent', 'evento': {
				'id': 'w1', 'status': 'sent',
				'pricing': {'pricing_model': 'PMP', 'type': 'regular', 'category': 'utility'},
			}},
			{'received_at': '2026-10-31T12:01:00+00:00', 'status': 'delivered', 'evento': {'id': 'w1', 'status': 'delivered'}},
		]})
		sem_pricing = _enviada(pessoa, metadata={'meta': {}, 'meta_webhook': [
			{'received_at': '2026-10-31T12:00:00+00:00', 'status': 'failed', 'evento': {'id': 'w2', 'status': 'failed'}},
		]})

		migracao._preencher_cobranca_do_historico(django_apps, None)

		com_pricing.refresh_from_db()
		sem_pricing.refresh_from_db()
		self.assertEqual(com_pricing.cobranca_categoria, 'utility')
		self.assertEqual(com_pricing.cobranca_tipo, 'regular')
		self.assertEqual(com_pricing.cobranca_modelo, 'PMP')
		self.assertIsNotNone(com_pricing.cobranca_registrada_em)
		self.assertEqual(sem_pricing.cobranca_categoria, '')


# ---------------------------------------------------------------------------
# Tela (somente administrador)
# ---------------------------------------------------------------------------
@override_settings(**META, STORAGES=SIMPLE_STATIC_STORAGES)
class RelatorioCustoViewTests(TestCase):
	def setUp(self):
		cache.clear()
		self.url = reverse('relatorio-custo-whatsapp')
		self.admin = get_user_model().objects.create_superuser('admin_custo', 'admin_custo@ex.com', 'x')
		self.staff = get_user_model().objects.create_user('staff_custo', password='x', is_staff=True)
		self.comum = get_user_model().objects.create_user('comum_custo', password='x')
		self.pessoa = _pessoa(nome='Ana Custo')
		_enviada(self.pessoa, categoria='marketing', tipo='regular', wamid='wamid.V1',
			metadata={'meta': {}, 'tipo_template': 'marketing_massa'})
		_precos(preco_marketing='0.3500')
		self.oficial = patch('apps.acolhimento.meta_service.get_pricing_analytics', return_value=[
			{'pricing_category': 'MARKETING', 'pricing_type': 'REGULAR', 'volume': 1, 'cost': 0.33},
		])
		self.oficial.start()
		self.addCleanup(self.oficial.stop)

	def test_anonimo_vai_para_o_login(self):
		resp = self.client.get(self.url)
		self.assertEqual(resp.status_code, 302)
		self.assertIn(reverse('login'), resp['Location'])

	def test_staff_e_usuario_comum_sao_bloqueados(self):
		for usuario in (self.staff, self.comum):
			self.client.force_login(usuario)
			self.assertEqual(self.client.get(self.url).status_code, 403)
			self.assertEqual(self.client.get(self.url, {'exportar': 'csv'}).status_code, 403)
			self.assertEqual(self.client.post(self.url, {'moeda': 'USD'}).status_code, 403)
		self.assertEqual(TabelaPrecoWhatsapp.carregar().moeda, 'BRL')

	def test_admin_ve_o_relatorio(self):
		self.client.force_login(self.admin)
		resp = self.client.get(self.url)
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Custo do WhatsApp')
		self.assertEqual(resp.context['total'].cobradas, 1)
		self.assertEqual(resp.context['total'].custo_estimado, Decimal('0.3500'))
		self.assertEqual(resp.context['oficial']['custo'], Decimal('0.33'))
		self.assertContains(resp, 'Disparo em massa (template)')

	def test_link_no_menu_so_para_admin(self):
		self.client.force_login(self.admin)
		self.assertContains(self.client.get(reverse('dashboard')), self.url)
		self.client.force_login(self.staff)
		self.assertNotContains(self.client.get(reverse('dashboard')), self.url)

	def test_exporta_csv(self):
		self.client.force_login(self.admin)
		resp = self.client.get(self.url, {'exportar': 'csv'})
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp['Content-Type'], 'text/csv; charset=utf-8')
		self.assertIn('attachment;', resp['Content-Disposition'])
		conteudo = resp.content.decode('utf-8-sig')
		self.assertIn('Ana Custo', conteudo)
		self.assertIn('Marketing', conteudo)
		self.assertIn('0,3500', conteudo)
		self.assertIn('wamid.V1', conteudo)

	def test_admin_salva_tabela_de_precos(self):
		self.client.force_login(self.admin)
		resp = self.client.post(self.url, {
			'moeda': 'BRL', 'preco_marketing': '0.4000', 'preco_utility': '0.0400',
			'preco_authentication': '0.0400', 'preco_service': '0.0200',
		})
		self.assertEqual(resp.status_code, 302)
		tabela = TabelaPrecoWhatsapp.carregar()
		self.assertEqual(tabela.preco_marketing, Decimal('0.4000'))
		self.assertEqual(tabela.atualizado_por, self.admin)

	def test_preco_negativo_e_recusado(self):
		self.client.force_login(self.admin)
		resp = self.client.post(self.url, {
			'moeda': 'BRL', 'preco_marketing': '-1', 'preco_utility': '0',
			'preco_authentication': '0', 'preco_service': '0',
		})
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'nao pode ser negativo')
		self.assertEqual(TabelaPrecoWhatsapp.carregar().preco_marketing, Decimal('0.3500'))

	def test_periodo_invalido_mostra_erro_e_usa_o_padrao(self):
		self.client.force_login(self.admin)
		resp = self.client.get(self.url, {'inicio': '2026-10-31', 'fim': '2026-10-01'})
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'igual ou posterior')
		self.assertEqual(resp.context['inicio'], custo_whatsapp.periodo_padrao()[0])

	def test_atalho_mes_anterior(self):
		self.client.force_login(self.admin)
		resp = self.client.get(self.url, {'atalho': 'mes_anterior'})
		self.assertEqual(
			(resp.context['inicio'], resp.context['fim']),
			custo_whatsapp.periodo_mes_anterior(),
		)
