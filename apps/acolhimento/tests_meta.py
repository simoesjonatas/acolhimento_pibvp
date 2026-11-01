"""Testes do provider `meta` (WhatsApp Cloud API / API oficial da Meta).

Cobrem o servico HTTP (payloads e erros), o gateway, a resolucao de templates, o
webhook (verificacao, assinatura, entrada, status), as telas que enfileiram
templates e os system checks. Nenhum teste toca a rede: urllib e mockado.
"""
import hashlib
import hmac
import io
import json
import urllib.error
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import meta_service, template_config, whatsapp_gateway
from apps.acolhimento.models import (
	ConfiguracaoAtendimentoBot,
	ConfiguracaoProcessamentoFila,
	InteracaoAcolhimento,
	MensagemContato,
	PrimeiroContato,
	TemplateWhatsapp,
)

SIMPLE_STATIC_STORAGES = {
	'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
	'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

META_OK = dict(
	WHATSAPP_PROVIDER='meta',
	META_WA_ACCESS_TOKEN='token-teste',
	META_WA_PHONE_NUMBER_ID='1234567890',
	META_WA_BUSINESS_ACCOUNT_ID='9876543210',
	META_WA_APP_SECRET='app-secret-teste',
	META_WA_VERIFY_TOKEN='verify-teste',
	META_TEMPLATE_OPT_IN_NOME='',
	META_TEMPLATE_CONTINUAR_NOME='',
)


def _pessoa(nome='Maria Teste', telefone='21971347985', **extra):
	dados = dict(
		nome=nome,
		telefone_whatsapp=telefone,
		primeira_vez=True,
		como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
		status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
	)
	dados.update(extra)
	return PrimeiroContato.objects.create(**dados)


def _saida(pessoa, **extra):
	dados = dict(
		pessoa=pessoa,
		canal=MensagemContato.CanalChoices.WHATSAPP,
		direcao=MensagemContato.DirecaoChoices.SAIDA,
		status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
		conteudo='ola',
	)
	dados.update(extra)
	return MensagemContato.objects.create(**dados)


class _RespostaFake:
	def __init__(self, status, body):
		self.status = status
		self._body = json.dumps(body).encode('utf-8')

	def read(self):
		return self._body

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False


def _http_error(status, body):
	return urllib.error.HTTPError(
		url='https://graph.facebook.com/x',
		code=status,
		msg='erro',
		hdrs=None,
		fp=io.BytesIO(json.dumps(body).encode('utf-8')),
	)


# ---------------------------------------------------------------------------
# meta_service
# ---------------------------------------------------------------------------
@override_settings(**META_OK)
class MetaServiceTests(TestCase):
	def _capturar(self, resposta):
		"""Mocka urlopen devolvendo `resposta` e captura a Request enviada."""
		capturado = {}

		def fake_urlopen(req, timeout=None):
			capturado['url'] = req.full_url
			capturado['method'] = req.get_method()
			capturado['headers'] = dict(req.header_items())
			capturado['body'] = json.loads(req.data.decode('utf-8')) if req.data else None
			if isinstance(resposta, Exception):
				raise resposta
			return resposta

		return capturado, patch('apps.acolhimento.meta_service.urllib.request.urlopen', side_effect=fake_urlopen)

	def test_send_text_monta_payload_e_normaliza_resposta(self):
		capturado, mock = self._capturar(_RespostaFake(200, {
			'messaging_product': 'whatsapp',
			'contacts': [{'input': '5521971347985', 'wa_id': '5521971347985'}],
			'messages': [{'id': 'wamid.ABC', 'message_status': 'accepted'}],
		}))
		with mock:
			resultado = meta_service.send_text(to_phone='+55 (21) 97134-7985', text='ola')

		self.assertEqual(capturado['method'], 'POST')
		self.assertTrue(capturado['url'].endswith('/v22.0/1234567890/messages'))
		self.assertEqual(capturado['headers'].get('Authorization'), 'Bearer token-teste')
		self.assertEqual(capturado['body']['to'], '5521971347985')
		self.assertEqual(capturado['body']['type'], 'text')
		self.assertEqual(capturado['body']['text']['body'], 'ola')
		self.assertEqual(capturado['body']['messaging_product'], 'whatsapp')
		self.assertEqual(resultado['sid'], 'wamid.ABC')
		self.assertEqual(resultado['status'], 'accepted')
		self.assertEqual(resultado['wa_id'], '5521971347985')

	def test_send_template_com_parametros_posicionais(self):
		capturado, mock = self._capturar(_RespostaFake(200, {'messages': [{'id': 'wamid.T'}]}))
		with mock:
			meta_service.send_template(
				to_phone='5521971347985', nome='boas_vindas', idioma='pt_BR',
				parametros={'2': 'PIBVP', '1': 'Maria'},
			)

		template = capturado['body']['template']
		self.assertEqual(template['name'], 'boas_vindas')
		self.assertEqual(template['language'], {'code': 'pt_BR'})
		parametros = template['components'][0]['parameters']
		# Ordem numerica, independente da ordem do dict.
		self.assertEqual([p['text'] for p in parametros], ['Maria', 'PIBVP'])
		self.assertNotIn('parameter_name', parametros[0])

	def test_send_template_com_parametros_nomeados(self):
		componentes = meta_service.montar_componentes({'nome': 'Maria', 'igreja': 'PIBVP'})
		parametros = componentes[0]['parameters']
		self.assertEqual(parametros[0]['parameter_name'], 'nome')
		self.assertEqual(parametros[0]['text'], 'Maria')

	def test_send_template_sem_parametros_nao_manda_components(self):
		capturado, mock = self._capturar(_RespostaFake(200, {'messages': [{'id': 'wamid.T'}]}))
		with mock:
			meta_service.send_template(to_phone='5521971347985', nome='sem_vars')
		self.assertNotIn('components', capturado['body']['template'])

	def test_erro_do_graph_vira_mensagem_amigavel(self):
		erro = _http_error(400, {'error': {
			'message': '(#131047) Re-engagement message',
			'type': 'OAuthException',
			'code': 131047,
			'error_data': {'details': 'Message failed to send because more than 24 hours have passed'},
		}})
		_capturado, mock = self._capturar(erro)
		with mock:
			with self.assertRaises(meta_service.MetaWhatsAppError) as ctx:
				meta_service.send_text(to_phone='5521971347985', text='ola')

		self.assertEqual(ctx.exception.code, 131047)
		self.assertIn('janela de 24h', str(ctx.exception))
		self.assertIn('131047', str(ctx.exception))

	def test_token_invalido_explica(self):
		erro = _http_error(401, {'error': {'message': 'Invalid OAuth access token', 'code': 190}})
		_capturado, mock = self._capturar(erro)
		with mock:
			with self.assertRaises(meta_service.MetaWhatsAppError) as ctx:
				meta_service.send_text(to_phone='5521971347985', text='ola')
		self.assertIn('Token de acesso invalido', str(ctx.exception))

	def test_sem_id_de_mensagem_e_erro(self):
		_capturado, mock = self._capturar(_RespostaFake(200, {'messages': []}))
		with mock:
			with self.assertRaises(meta_service.MetaWhatsAppError):
				meta_service.send_text(to_phone='5521971347985', text='ola')

	@override_settings(META_WA_ACCESS_TOKEN='')
	def test_sem_token_falha_antes_da_rede(self):
		with patch('apps.acolhimento.meta_service.urllib.request.urlopen') as urlopen:
			with self.assertRaises(meta_service.MetaWhatsAppError):
				meta_service.send_text(to_phone='5521971347985', text='ola')
		urlopen.assert_not_called()
		self.assertFalse(meta_service.configurado())

	def test_marcar_como_lida_manda_status_read(self):
		capturado, mock = self._capturar(_RespostaFake(200, {'success': True}))
		with mock:
			self.assertTrue(meta_service.marcar_como_lida('wamid.IN'))
		self.assertTrue(capturado['url'].endswith('/1234567890/messages'))
		self.assertEqual(capturado['body'], {
			'messaging_product': 'whatsapp', 'status': 'read', 'message_id': 'wamid.IN',
		})

	def test_marcar_como_lida_com_indicador_de_digitacao(self):
		capturado, mock = self._capturar(_RespostaFake(200, {'success': True}))
		with mock:
			meta_service.marcar_como_lida('wamid.IN', digitando=True)
		self.assertEqual(capturado['body']['typing_indicator'], {'type': 'text'})

	def test_marcar_como_lida_falha_devolve_false(self):
		_capturado, mock = self._capturar(_http_error(400, {'error': {'code': 100, 'message': 'x'}}))
		with mock:
			self.assertFalse(meta_service.marcar_como_lida('wamid.IN'))

	def test_get_phone_number_info_devolve_none_em_falha(self):
		_capturado, mock = self._capturar(_http_error(400, {'error': {'message': 'x', 'code': 100}}))
		with mock:
			self.assertIsNone(meta_service.get_phone_number_info())

	def test_list_templates_le_data(self):
		_capturado, mock = self._capturar(_RespostaFake(200, {'data': [
			{'name': 'boas_vindas', 'language': 'pt_BR', 'status': 'APPROVED', 'category': 'UTILITY'},
		]}))
		with mock:
			templates = meta_service.list_templates()
		self.assertEqual(templates[0]['name'], 'boas_vindas')


# ---------------------------------------------------------------------------
# template_config + gateway
# ---------------------------------------------------------------------------
@override_settings(**META_OK)
class MetaTemplateConfigTests(TestCase):
	def test_configurado_para_usa_nome_do_template(self):
		self.assertTrue(template_config.usando_meta())
		self.assertFalse(template_config.opt_in_configurado())
		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, meta_nome='boas_vindas')
		self.assertTrue(template_config.opt_in_configurado())
		self.assertEqual(template_config.opt_in_meta_idioma(), 'pt_BR')

	@override_settings(META_TEMPLATE_CONTINUAR_NOME='continuar_env', META_TEMPLATE_CONTINUAR_IDIOMA='en_US')
	def test_fallback_env(self):
		self.assertEqual(template_config.continuar_meta_nome(), 'continuar_env')
		self.assertEqual(template_config.continuar_meta_idioma(), 'en_US')
		self.assertTrue(template_config.continuar_configurado())

	def test_montar_meta_template_resolve_nome(self):
		TemplateWhatsapp.objects.create(
			tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO,
			meta_nome='boas_vindas',
			content_variables='{"2": "Igreja {nome}"}',
		)
		pessoa = _pessoa(nome='Joao')
		bloco = template_config.montar_meta_template(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, pessoa)
		self.assertEqual(bloco['nome'], 'boas_vindas')
		self.assertEqual(bloco['idioma'], 'pt_BR')
		self.assertEqual(bloco['parametros'], {'1': 'Joao', '2': 'Igreja Joao'})

	def test_variaveis_do_env_da_meta_nao_da_twilio(self):
		with override_settings(
			TWILIO_TEMPLATE_OPT_IN_VARIABLES='{"9": "twilio"}',
			META_TEMPLATE_OPT_IN_VARIABLES='{"9": "meta"}',
		):
			self.assertEqual(template_config.variaveis_dict_para(TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO), {'9': 'meta'})


@override_settings(**META_OK)
class MetaGatewayTests(TestCase):
	def test_provider_key_e_regras(self):
		self.assertTrue(whatsapp_gateway.usando_meta())
		self.assertFalse(whatsapp_gateway.usando_evolution())
		self.assertEqual(whatsapp_gateway.provider_key(), 'meta')

	def test_regras_de_bloqueio_se_aplicam_no_modo_meta(self):
		"""Diferente do Evolution, a API oficial segue opt-in/janela de 24h."""
		msg = _saida(_pessoa())
		with patch('apps.acolhimento.whatsapp_rules.motivo_bloqueio_mensagem', return_value=('x', 'bloqueado')) as rules:
			self.assertEqual(whatsapp_gateway.motivo_bloqueio_mensagem(msg), ('x', 'bloqueado'))
		rules.assert_called_once()

	def test_texto_livre_usa_send_text(self):
		msg = _saida(_pessoa(), conteudo='Oi, tudo bem?')
		with patch('apps.acolhimento.meta_service.send_text') as send_text:
			send_text.return_value = {'sid': 'wamid.1', 'status': 'accepted', 'to': '5521971347985', 'raw': {}}
			envelope = whatsapp_gateway.enviar(msg, '+5521971347985')

		send_text.assert_called_once_with(to_phone='+5521971347985', text='Oi, tudo bem?')
		self.assertEqual(envelope['provider'], 'meta')
		self.assertEqual(envelope['status_fila'], MensagemContato.StatusFilaChoices.ENVIADA)
		self.assertEqual(envelope['referencia_externa'], 'wamid.1')
		self.assertEqual(envelope['metadata_key'], 'meta')
		self.assertIsNotNone(envelope['enviada_em'])

	def test_template_gravado_na_mensagem_usa_send_template(self):
		msg = _saida(_pessoa(), metadata_envio={
			'tipo_template': 'marketing_massa',
			'meta_template': {'nome': 'convite', 'idioma': 'pt_BR', 'parametros': {'1': 'Maria'}},
		})
		with patch('apps.acolhimento.meta_service.send_template') as send_template:
			send_template.return_value = {'sid': 'wamid.2', 'status': 'accepted', 'to': 'x', 'raw': {}}
			whatsapp_gateway.enviar(msg, '+5521971347985')

		send_template.assert_called_once_with(
			to_phone='+5521971347985', nome='convite', idioma='pt_BR', parametros={'1': 'Maria'}
		)

	def test_opt_in_sem_bloco_meta_resolve_pela_configuracao(self):
		"""Fila enfileirada em outro provider (ex.: migracao): resolve o template na hora."""
		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, meta_nome='boas_vindas')
		msg = _saida(_pessoa(nome='Ana'), metadata_envio={'tipo_template': 'primeiro_contato_opt_in'})
		with patch('apps.acolhimento.meta_service.send_template') as send_template:
			send_template.return_value = {'sid': 'wamid.3', 'status': 'accepted', 'to': 'x', 'raw': {}}
			whatsapp_gateway.enviar(msg, '+5521971347985')

		kwargs = send_template.call_args.kwargs
		self.assertEqual(kwargs['nome'], 'boas_vindas')
		self.assertEqual(kwargs['parametros']['1'], 'Ana')

	def test_opt_in_sem_template_configurado_falha_com_mensagem_clara(self):
		msg = _saida(_pessoa(), metadata_envio={'tipo_template': 'primeiro_contato_opt_in'})
		with patch('apps.acolhimento.meta_service.send_template') as send_template:
			with self.assertRaises(whatsapp_gateway.WhatsAppSendError) as ctx:
				whatsapp_gateway.enviar(msg, '+5521971347985')
		send_template.assert_not_called()
		self.assertIn('nao configurado', str(ctx.exception))

	def test_erro_da_meta_vira_whatsapp_send_error(self):
		msg = _saida(_pessoa())
		with patch('apps.acolhimento.meta_service.send_text', side_effect=meta_service.MetaWhatsAppError('recusou', code=131047)):
			with self.assertRaises(whatsapp_gateway.WhatsAppSendError):
				whatsapp_gateway.enviar(msg, '+5521971347985')

	def test_fila_processa_pelo_gateway_meta(self):
		from apps.acolhimento.fila_processor import processar_fila_mensagens

		ConfiguracaoProcessamentoFila.carregar()
		msg = _saida(_pessoa(iniciou_interacao=True), conteudo='ola')
		with patch('apps.acolhimento.meta_service.send_text') as send_text, patch('apps.acolhimento.fila_processor.time.sleep'):
			send_text.return_value = {'sid': 'wamid.F', 'status': 'accepted', 'to': 'x', 'raw': {'ok': True}}
			resultado = processar_fila_mensagens(ids=[msg.id])

		self.assertEqual(resultado['sucesso'], 1)
		msg.refresh_from_db()
		self.assertEqual(msg.status_fila, MensagemContato.StatusFilaChoices.ENVIADA)
		self.assertEqual(msg.referencia_externa, 'wamid.F')
		self.assertEqual(msg.metadata_envio['meta']['sid'], 'wamid.F')


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------
@override_settings(**META_OK, DEBUG=False)
class MetaWebhookTests(TestCase):
	def setUp(self):
		self.url = reverse('mensagens-webhook-meta')
		# Toda entrada recebe o tique azul: sem este mock o teste iria a rede.
		patcher = patch('apps.acolhimento.meta_service.marcar_como_lida', return_value=True)
		self.marcar_como_lida = patcher.start()
		self.addCleanup(patcher.stop)

	def _assinar(self, corpo: bytes) -> str:
		return 'sha256=' + hmac.new(b'app-secret-teste', corpo, hashlib.sha256).hexdigest()

	def _postar(self, payload, assinatura=None):
		corpo = json.dumps(payload).encode('utf-8')
		return self.client.post(
			self.url,
			data=corpo,
			content_type='application/json',
			HTTP_X_HUB_SIGNATURE_256=assinatura if assinatura is not None else self._assinar(corpo),
		)

	def _evento(self, *, messages=None, statuses=None, contacts=None):
		value = {
			'messaging_product': 'whatsapp',
			'metadata': {'display_phone_number': '5521900000000', 'phone_number_id': '1234567890'},
		}
		if contacts is not None:
			value['contacts'] = contacts
		if messages is not None:
			value['messages'] = messages
		if statuses is not None:
			value['statuses'] = statuses
		return {
			'object': 'whatsapp_business_account',
			'entry': [{'id': '9876543210', 'changes': [{'field': 'messages', 'value': value}]}],
		}

	def _texto(self, texto='oi', wamid='wamid.IN1', numero='5521971347985', nome='Jonatas'):
		return self._evento(
			contacts=[{'profile': {'name': nome}, 'wa_id': numero}],
			messages=[{'from': numero, 'id': wamid, 'timestamp': '1700000000', 'type': 'text', 'text': {'body': texto}}],
		)

	# --- verificacao (GET)
	def test_get_verificacao_devolve_challenge(self):
		resp = self.client.get(self.url, {
			'hub.mode': 'subscribe', 'hub.verify_token': 'verify-teste', 'hub.challenge': '424242',
		})
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.content.decode(), '424242')

	def test_get_verificacao_token_errado_403(self):
		resp = self.client.get(self.url, {
			'hub.mode': 'subscribe', 'hub.verify_token': 'errado', 'hub.challenge': '424242',
		})
		self.assertEqual(resp.status_code, 403)

	@override_settings(META_WA_VERIFY_TOKEN='')
	def test_get_verificacao_sem_token_configurado_403(self):
		resp = self.client.get(self.url, {
			'hub.mode': 'subscribe', 'hub.verify_token': '', 'hub.challenge': '1',
		})
		self.assertEqual(resp.status_code, 403)

	# --- assinatura
	def test_post_sem_assinatura_403(self):
		resp = self._postar(self._texto(), assinatura='')
		self.assertEqual(resp.status_code, 403)
		self.assertEqual(PrimeiroContato.objects.count(), 0)

	def test_post_assinatura_errada_403(self):
		resp = self._postar(self._texto(), assinatura='sha256=' + '0' * 64)
		self.assertEqual(resp.status_code, 403)

	@override_settings(META_WA_APP_SECRET='')
	def test_post_sem_app_secret_em_producao_403(self):
		resp = self._postar(self._texto(), assinatura='')
		self.assertEqual(resp.status_code, 403)

	@override_settings(META_WA_APP_SECRET='', DEBUG=True)
	def test_post_sem_app_secret_em_debug_libera(self):
		resp = self._postar(self._texto(), assinatura='')
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(PrimeiroContato.objects.count(), 1)

	# --- entrada
	def test_entrada_cria_pessoa_e_mensagem(self):
		resp = self._postar(self._texto('Ola, quero conhecer a igreja'))
		self.assertEqual(resp.status_code, 200)

		pessoa = PrimeiroContato.objects.get()
		self.assertEqual(pessoa.nome, 'Jonatas')
		self.assertTrue(pessoa.iniciou_interacao)
		self.assertEqual(pessoa.origem_cadastro, PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO)
		entrada = MensagemContato.objects.get(direcao=MensagemContato.DirecaoChoices.ENTRADA)
		self.assertEqual(entrada.conteudo, 'Ola, quero conhecer a igreja')
		self.assertEqual(entrada.referencia_externa, 'wamid.IN1')
		self.assertEqual(entrada.metadata_resposta['meta']['type'], 'text')

	def test_entrada_reconhece_pessoa_pelo_telefone_e_libera_envio(self):
		pessoa = _pessoa(nome='Maria', telefone='21971347985', iniciou_interacao=False)
		saida = _saida(pessoa, status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			metadata_envio={'tipo_template': 'primeiro_contato_opt_in'})

		resp = self._postar(self._texto('sim, podemos'))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(PrimeiroContato.objects.count(), 1)
		pessoa.refresh_from_db()
		self.assertTrue(pessoa.iniciou_interacao)
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		saida.refresh_from_db()
		self.assertEqual(saida.resposta_conteudo, 'sim, podemos')
		self.assertIsNotNone(saida.resposta_recebida_em)
		self.assertEqual(len(saida.metadata_resposta['meta_webhook']), 1)

	def test_entrada_duplicada_e_ignorada(self):
		self._postar(self._texto(wamid='wamid.DUP'))
		self._postar(self._texto(wamid='wamid.DUP'))
		self.assertEqual(MensagemContato.objects.filter(direcao=MensagemContato.DirecaoChoices.ENTRADA).count(), 1)

	def test_entrada_de_botao_e_midia(self):
		payload = self._evento(messages=[
			{'from': '5521971347985', 'id': 'wamid.B', 'type': 'button', 'button': {'text': 'Sim', 'payload': 'SIM'}},
			{'from': '5521971347985', 'id': 'wamid.A', 'type': 'audio', 'audio': {'id': '1'}},
			{'from': '5521971347985', 'id': 'wamid.I', 'type': 'image', 'image': {'id': '2', 'caption': 'foto'}},
		])
		resp = self._postar(payload)
		self.assertEqual(resp.status_code, 200)
		conteudos = set(MensagemContato.objects.values_list('conteudo', flat=True))
		self.assertEqual(conteudos, {'Sim', '(audio)', 'foto'})
		self.assertEqual(PrimeiroContato.objects.count(), 1)

	def test_entrada_aciona_bot(self):
		with patch('apps.acolhimento.meta_webhooks.atendimento_bot.processar_entrada', return_value=True) as bot:
			self._postar(self._texto('1'))
		bot.assert_called_once()
		self.assertEqual(bot.call_args.args[1], '1')

	# --- botoes de resposta rapida dos templates
	def _botao(self, texto, payload=None, wamid='wamid.BT1', numero='5521971347985'):
		return self._evento(
			contacts=[{'profile': {'name': 'Maria'}, 'wa_id': numero}],
			messages=[{
				'from': numero, 'id': wamid, 'timestamp': '1700000000', 'type': 'button',
				'context': {'from': '5521900000000', 'id': 'wamid.TEMPLATE'},
				'button': {'text': texto, 'payload': texto if payload is None else payload},
			}],
		)

	def _botao_interativo(self, id_botao, titulo, wamid='wamid.BT2', numero='5521971347985'):
		return self._evento(messages=[{
			'from': numero, 'id': wamid, 'type': 'interactive',
			'interactive': {'type': 'button_reply', 'button_reply': {'id': id_botao, 'title': titulo}},
		}])

	def _ligar_bot(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.save()

	def _saidas_do_bot(self, pessoa):
		return MensagemContato.objects.filter(
			pessoa=pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA,
			metadata_envio__origem='bot_atendimento',
		)

	def test_botao_sim_poe_em_acompanhamento_sem_acionar_bot(self):
		self._ligar_bot()
		pessoa = _pessoa(nome='Maria', iniciou_interacao=False)
		_saida(pessoa, status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			metadata_envio={'tipo_template': 'primeiro_contato_opt_in'})

		resp = self._postar(self._botao('Sim, podemos'))
		self.assertEqual(resp.status_code, 200)

		pessoa.refresh_from_db()
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		self.assertTrue(pessoa.iniciou_interacao)
		self.assertEqual(self._saidas_do_bot(pessoa).count(), 0)
		self.assertEqual(pessoa.bot_etapa, PrimeiroContato.BotEtapaChoices.INATIVO)

		entrada = MensagemContato.objects.get(direcao=MensagemContato.DirecaoChoices.ENTRADA)
		self.assertEqual(entrada.conteudo, 'Sim, podemos')
		self.assertEqual(entrada.metadata_resposta['meta_botao'], 'sim')
		self.assertEqual(entrada.metadata_resposta['meta_botao_texto'], 'Sim, podemos')
		# A equipe precisa dar sequencia: continua na fila de retorno pendente.
		self.assertIsNone(entrada.visualizada_equipe_em)
		self.assertTrue(InteracaoAcolhimento.objects.filter(
			pessoa=pessoa,
			tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
			descricao__contains='Em acompanhamento',
		).exists())
		self.marcar_como_lida.assert_called_once_with('wamid.BT1', digitando=False)

	def test_botao_sim_nao_rebaixa_quem_ja_esta_adiante(self):
		pessoa = _pessoa(status=PrimeiroContato.StatusAcolhimento.MEMBRO, iniciou_interacao=True)
		with patch('apps.acolhimento.meta_webhooks.atendimento_bot.processar_entrada') as bot:
			self._postar(self._botao('Sim, podemos'))
		bot.assert_not_called()
		pessoa.refresh_from_db()
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.MEMBRO)

	def test_botao_interativo_com_id_sim(self):
		pessoa = _pessoa(status=PrimeiroContato.StatusAcolhimento.ROBO, iniciou_interacao=True)
		with patch('apps.acolhimento.meta_webhooks.atendimento_bot.processar_entrada') as bot:
			self._postar(self._botao_interativo('sim', 'Bora!'))
		bot.assert_not_called()
		pessoa.refresh_from_db()
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		entrada = MensagemContato.objects.get(direcao=MensagemContato.DirecaoChoices.ENTRADA)
		self.assertEqual(entrada.metadata_resposta['meta_botao'], 'sim')
		self.assertEqual(entrada.metadata_resposta['meta_botao_texto'], 'Bora!')

	def test_botao_agora_nao_registra_sem_poluir_retorno_pendente(self):
		self._ligar_bot()
		pessoa = _pessoa(nome='Maria', iniciou_interacao=False)

		resp = self._postar(self._botao('Agora não'))
		self.assertEqual(resp.status_code, 200)

		pessoa.refresh_from_db()
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO)
		self.assertEqual(self._saidas_do_bot(pessoa).count(), 0)
		entrada = MensagemContato.objects.get(direcao=MensagemContato.DirecaoChoices.ENTRADA)
		self.assertEqual(entrada.metadata_resposta['meta_botao'], 'agora_nao')
		self.assertIsNotNone(entrada.visualizada_equipe_em)
		self.assertFalse(MensagemContato.objects.filter(
			direcao=MensagemContato.DirecaoChoices.ENTRADA, visualizada_equipe_em__isnull=True,
		).exists())
		self.assertTrue(InteracaoAcolhimento.objects.filter(
			pessoa=pessoa,
			tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
			descricao__contains='pediu para falar depois',
		).exists())
		self.marcar_como_lida.assert_called_once_with('wamid.BT1', digitando=False)

	def test_botao_agora_nao_pelo_id_interativo(self):
		_pessoa(iniciou_interacao=True)
		with patch('apps.acolhimento.meta_webhooks.atendimento_bot.processar_entrada') as bot:
			self._postar(self._botao_interativo('nao', 'Depois'))
		bot.assert_not_called()
		entrada = MensagemContato.objects.get(direcao=MensagemContato.DirecaoChoices.ENTRADA)
		self.assertEqual(entrada.metadata_resposta['meta_botao'], 'agora_nao')

	@override_settings(META_BOTAO_SIM='Quero sim', META_BOTAO_NAO='Mais tarde')
	def test_textos_dos_botoes_vem_das_settings(self):
		pessoa = _pessoa(iniciou_interacao=True)
		self._postar(self._botao('Mais tarde', payload='', wamid='wamid.N'))
		self._postar(self._botao('Quero sim!', payload='', wamid='wamid.S'))
		marcadores = dict(
			(m.referencia_externa, m.metadata_resposta['meta_botao'])
			for m in MensagemContato.objects.filter(direcao=MensagemContato.DirecaoChoices.ENTRADA)
		)
		self.assertEqual(marcadores, {'wamid.N': 'agora_nao', 'wamid.S': 'sim'})
		pessoa.refresh_from_db()
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)

	def test_botao_desconhecido_segue_fluxo_normal_e_bot(self):
		pessoa = _pessoa(iniciou_interacao=True)
		with patch('apps.acolhimento.meta_webhooks.atendimento_bot.processar_entrada', return_value=True) as bot:
			self._postar(self._botao('Quero saber os horarios'))
		bot.assert_called_once()
		self.assertEqual(bot.call_args.args[1], 'Quero saber os horarios')
		pessoa.refresh_from_db()
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO)
		entrada = MensagemContato.objects.get(direcao=MensagemContato.DirecaoChoices.ENTRADA)
		self.assertEqual(entrada.metadata_resposta['meta_botao'], 'desconhecido')
		self.assertIsNone(entrada.visualizada_equipe_em)
		self.assertFalse(InteracaoAcolhimento.objects.filter(pessoa=pessoa).exists())

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_telas_mostram_botao_e_pedido_para_falar_depois(self):
		pessoa = _pessoa(iniciou_interacao=True)
		self._postar(self._botao('Agora não'))
		self.client.force_login(get_user_model().objects.create_superuser('super_bt', 'bt@ex.com', 'x'))

		detalhe = self.client.get(reverse('pessoas-detalhe', args=[pessoa.pk]))
		self.assertContains(detalhe, 'Pediu para falar depois')
		conversa = self.client.get(reverse('pessoas-mensagens', args=[pessoa.pk]))
		self.assertContains(conversa, 'respondeu pelo botão: <strong>Agora não</strong>', html=False)

		# Depois que a pessoa volta a escrever, o aviso sai da tela de pessoa.
		self._postar(self._texto('oi, agora posso', wamid='wamid.DEPOIS'))
		detalhe = self.client.get(reverse('pessoas-detalhe', args=[pessoa.pk]))
		self.assertNotContains(detalhe, 'Pediu para falar depois')

	# --- marcar como lida (tique azul)
	def test_entrada_de_texto_e_marcada_como_lida(self):
		self._postar(self._texto(wamid='wamid.LIDA'))
		self.marcar_como_lida.assert_called_once_with('wamid.LIDA', digitando=False)

	def test_bot_respondendo_com_fila_ligada_mostra_digitando(self):
		cfg = ConfiguracaoProcessamentoFila.carregar()
		cfg.auto_ativo = True
		cfg.save()
		with patch('apps.acolhimento.meta_webhooks.atendimento_bot.processar_entrada', return_value=True):
			self._postar(self._texto(wamid='wamid.DIG'))
		self.marcar_como_lida.assert_called_once_with('wamid.DIG', digitando=True)

	def test_entrada_duplicada_nao_marca_de_novo(self):
		self._postar(self._texto(wamid='wamid.DUP2'))
		self._postar(self._texto(wamid='wamid.DUP2'))
		self.assertEqual(self.marcar_como_lida.call_count, 1)

	def test_falha_ao_marcar_como_lida_nao_derruba_webhook(self):
		self.marcar_como_lida.side_effect = TimeoutError('timed out')
		resp = self._postar(self._texto(wamid='wamid.TO'))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.json()['entradas'], 1)

	def test_payload_de_outro_objeto_e_ignorado(self):
		resp = self._postar({'object': 'page', 'entry': []})
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(PrimeiroContato.objects.count(), 0)

	def test_json_invalido_400(self):
		corpo = b'{nao e json'
		resp = self.client.post(self.url, data=corpo, content_type='application/json',
			HTTP_X_HUB_SIGNATURE_256=self._assinar(corpo))
		self.assertEqual(resp.status_code, 400)

	# --- status
	def _status(self, wamid, status, errors=None):
		item = {'id': wamid, 'status': status, 'timestamp': '1700000000', 'recipient_id': '5521971347985'}
		if errors:
			item['errors'] = errors
		return self._evento(statuses=[item])

	def test_status_delivered_e_read_marcam_mensagem(self):
		msg = _saida(_pessoa(), status_fila=MensagemContato.StatusFilaChoices.ENVIADA, referencia_externa='wamid.S1')

		self._postar(self._status('wamid.S1', 'delivered'))
		msg.refresh_from_db()
		self.assertIsNotNone(msg.entregue_em)
		self.assertIsNone(msg.lida_em)

		self._postar(self._status('wamid.S1', 'read'))
		msg.refresh_from_db()
		self.assertIsNotNone(msg.lida_em)
		self.assertEqual(len(msg.metadata_envio['meta_webhook']), 2)

	def test_status_failed_vira_falha_com_motivo(self):
		msg = _saida(_pessoa(), status_fila=MensagemContato.StatusFilaChoices.ENVIADA, referencia_externa='wamid.S2')
		resp = self._postar(self._status('wamid.S2', 'failed', errors=[
			{'code': 131026, 'title': 'Message undeliverable', 'error_data': {'details': 'Receiver incapable'}},
		]))
		self.assertEqual(resp.status_code, 200)
		msg.refresh_from_db()
		self.assertEqual(msg.status_fila, MensagemContato.StatusFilaChoices.FALHA)
		self.assertIn('131026', msg.erro_ultimo_envio)
		self.assertIn('Receiver incapable', msg.erro_ultimo_envio)

	def test_status_de_mensagem_desconhecida_nao_quebra(self):
		resp = self._postar(self._status('wamid.NAOEXISTE', 'sent'))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.json()['status'], 0)

	def test_falhas_consecutivas_pausam_disparo_automatico(self):
		cfg = ConfiguracaoProcessamentoFila.carregar()
		cfg.auto_ativo = True
		cfg.save()
		pessoa = _pessoa()
		for indice in range(5):
			_saida(pessoa, status_fila=MensagemContato.StatusFilaChoices.ENVIADA, referencia_externa=f'wamid.X{indice}')
		for indice in range(5):
			self._postar(self._status(f'wamid.X{indice}', 'failed', errors=[{'code': 1, 'title': 'x'}]))
		cfg.refresh_from_db()
		self.assertFalse(cfg.auto_ativo)


# ---------------------------------------------------------------------------
# Telas que enfileiram templates
# ---------------------------------------------------------------------------
@override_settings(**META_OK)
class MetaEnfileiramentoViewsTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user('equipe_meta', password='x', is_staff=True)
		self.client.force_login(self.user)
		self.pessoa = _pessoa(nome='Visitante', telefone='31955554444')

	def test_dashboard_enfileira_opt_in_com_bloco_meta(self):
		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, meta_nome='boas_vindas', content_sid='HXignorado')

		resp = self.client.post(reverse('dashboard'), {'action': 'disparar_template_opt_in'})
		self.assertEqual(resp.status_code, 302)

		mensagem = MensagemContato.objects.get(pessoa=self.pessoa)
		self.assertEqual(mensagem.metadata_envio['tipo_template'], 'primeiro_contato_opt_in')
		self.assertEqual(mensagem.metadata_envio['meta_template']['nome'], 'boas_vindas')
		self.assertEqual(mensagem.metadata_envio['meta_template']['parametros']['1'], 'Visitante')
		self.assertNotIn('twilio_template', mensagem.metadata_envio)
		self.assertNotIn('evolution_texto', mensagem.metadata_envio)

	def test_dashboard_exige_nome_do_template(self):
		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, content_sid='HXsoTwilio')
		resp = self.client.post(reverse('dashboard'), {'action': 'disparar_template_opt_in'})
		self.assertEqual(resp.status_code, 302)
		self.assertFalse(MensagemContato.objects.filter(pessoa=self.pessoa).exists())

	def test_continuar_enfileira_com_bloco_meta(self):
		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.CONTINUAR, meta_nome='continuar', meta_idioma='pt_BR')
		self.pessoa.iniciou_interacao = True
		self.pessoa.save()

		resp = self.client.post(reverse('pessoas-template-continuar', args=[self.pessoa.pk]))
		self.assertEqual(resp.status_code, 302)
		mensagem = MensagemContato.objects.get(pessoa=self.pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA)
		self.assertEqual(mensagem.metadata_envio['tipo_template'], 'continuar_conversa')
		self.assertEqual(mensagem.metadata_envio['meta_template']['nome'], 'continuar')
		self.assertNotIn('twilio_template', mensagem.metadata_envio)

	def test_disparo_massa_template_usa_nome_da_meta(self):
		# O disparo por template so lista quem ja saiu do primeiro contato.
		self.pessoa.status = PrimeiroContato.StatusAcolhimento.PARTICIPANTE
		self.pessoa.save()
		resp = self.client.post(reverse('mensagens-disparo-massa'), {
			'modo': 'template',
			'content_sid': 'convite_evento',
			'template_idioma': 'pt_BR',
			'content_variables': '{"1": "{nome}"}',
			'pessoas': [self.pessoa.pk],
		})
		self.assertEqual(resp.status_code, 302)
		mensagem = MensagemContato.objects.get(pessoa=self.pessoa)
		self.assertEqual(mensagem.metadata_envio['tipo_template'], 'marketing_massa')
		self.assertEqual(mensagem.metadata_envio['meta_template'], {
			'nome': 'convite_evento', 'idioma': 'pt_BR', 'parametros': {'1': 'Visitante'},
		})
		self.assertNotIn('twilio_template', mensagem.metadata_envio)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_disparo_massa_rejeita_nome_invalido(self):
		self.pessoa.status = PrimeiroContato.StatusAcolhimento.PARTICIPANTE
		self.pessoa.save()
		resp = self.client.post(reverse('mensagens-disparo-massa'), {
			'modo': 'template',
			'content_sid': 'Nome Com Espaco',
			'pessoas': [self.pessoa.pk],
		})
		self.assertEqual(resp.status_code, 200)
		self.assertFalse(MensagemContato.objects.exists())
		self.assertContains(resp, 'letras minusculas')


@override_settings(**META_OK)
class MetaConfiguracaoTests(TestCase):
	def setUp(self):
		from django.core.cache import cache

		# O indicador do menu cacheia o estado por 30s; nao vazar entre testes.
		cache.clear()
		self.super = get_user_model().objects.create_superuser('super_meta', 'super_meta@ex.com', 'x')
		self.client.force_login(self.super)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_tela_de_templates_salva_nome_e_idioma(self):
		resp = self.client.post(reverse('configuracao-templates'), {
			'opt_in_sid': '', 'opt_in_variables': '{}', 'opt_in_texto_evolution': '',
			'opt_in_meta_nome': 'boas_vindas_pibvp', 'opt_in_meta_idioma': '',
			'continuar_sid': '', 'continuar_variables': '{}', 'continuar_texto_evolution': '',
			'continuar_meta_nome': 'continuar_pibvp', 'continuar_meta_idioma': 'en_US',
		})
		self.assertEqual(resp.status_code, 302)
		self.assertEqual(template_config.opt_in_meta_nome(), 'boas_vindas_pibvp')
		self.assertEqual(template_config.opt_in_meta_idioma(), 'pt_BR')
		self.assertEqual(template_config.continuar_meta_nome(), 'continuar_pibvp')
		self.assertEqual(template_config.continuar_meta_idioma(), 'en_US')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_tela_de_templates_rejeita_nome_invalido(self):
		resp = self.client.post(reverse('configuracao-templates'), {
			'opt_in_sid': '', 'opt_in_variables': '{}', 'opt_in_texto_evolution': '',
			'opt_in_meta_nome': 'Boas Vindas', 'opt_in_meta_idioma': 'portugues',
			'continuar_sid': '', 'continuar_variables': '{}', 'continuar_texto_evolution': '',
			'continuar_meta_nome': '', 'continuar_meta_idioma': '',
		})
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'letras minusculas')
		self.assertContains(resp, 'pt_BR')
		self.assertEqual(TemplateWhatsapp.objects.count(), 0)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_pagina_de_conexao_mostra_status_do_numero(self):
		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, meta_nome='boas_vindas')
		info = {'display_phone_number': '+55 21 90000-0000', 'verified_name': 'PIBVP', 'quality_rating': 'GREEN'}
		templates = [{'name': 'boas_vindas', 'language': 'pt_BR', 'status': 'APPROVED', 'category': 'UTILITY'}]
		with patch('apps.acolhimento.meta_service.get_phone_number_info', return_value=info), \
			patch('apps.acolhimento.meta_service.list_templates', return_value=templates):
			resp = self.client.get(reverse('configuracao-whatsapp'))

		self.assertEqual(resp.status_code, 200)
		self.assertTemplateUsed(resp, 'configuracao_whatsapp_meta.html')
		self.assertContains(resp, 'Conectado a Meta')
		self.assertContains(resp, 'PIBVP')
		self.assertContains(resp, 'boas_vindas')
		self.assertContains(resp, 'approved')
		# Token nunca aparece inteiro na tela.
		self.assertNotContains(resp, 'token-teste')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES, WHATSAPP_PROVIDER='evolution')
	def test_pagina_de_conexao_no_modo_evolution_continua_a_antiga(self):
		with patch('apps.acolhimento.evolution_service.get_connection_state', return_value='close'):
			resp = self.client.get(reverse('configuracao-whatsapp'))
		self.assertEqual(resp.status_code, 200)
		self.assertTemplateUsed(resp, 'configuracao_whatsapp.html')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_envio_de_teste_usa_template_de_opt_in(self):
		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, meta_nome='boas_vindas')
		with patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={}), \
			patch('apps.acolhimento.meta_service.list_templates', return_value=[]), \
			patch('apps.acolhimento.meta_service.send_template') as send_template:
			send_template.return_value = {'sid': 'wamid.TESTE', 'status': 'accepted', 'to': 'x', 'raw': {}}
			resp = self.client.post(reverse('configuracao-whatsapp'), {
				'acao': 'teste', 'telefone': '(21) 97134-7985', 'modo': 'template',
			})
		self.assertEqual(resp.status_code, 200)
		kwargs = send_template.call_args.kwargs
		self.assertEqual(kwargs['to_phone'], '+5521971347985')
		self.assertEqual(kwargs['nome'], 'boas_vindas')
		self.assertContains(resp, 'wamid.TESTE')

	def test_staff_nao_superuser_bloqueado(self):
		staff = get_user_model().objects.create_user('staff_meta', password='x', is_staff=True)
		self.client.force_login(staff)
		resp = self.client.get(reverse('configuracao-whatsapp'))
		self.assertEqual(resp.status_code, 403)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_indicador_do_menu_aparece_no_modo_meta(self):
		with patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={'ok': 1}):
			resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp.context['whatsapp_provider'], 'meta')
		self.assertEqual(resp.context['whatsapp_estado'], 'open')


class MetaChecksTests(TestCase):
	def _rodar(self):
		from apps.acolhimento import checks

		return [item.id for item in checks.meta_cloud_api_configurada(None)]

	@override_settings(WHATSAPP_PROVIDER='evolution')
	def test_sem_meta_nao_avisa(self):
		self.assertEqual(self._rodar(), [])

	@override_settings(WHATSAPP_PROVIDER='meta', META_WA_ACCESS_TOKEN='', META_WA_PHONE_NUMBER_ID='',
		META_WA_APP_SECRET='', META_WA_VERIFY_TOKEN='', DEBUG=False)
	def test_meta_sem_nada_avisa_tudo(self):
		ids = self._rodar()
		self.assertEqual(ids, ['acolhimento.W002', 'acolhimento.W003', 'acolhimento.W004'])

	@override_settings(**META_OK, DEBUG=False)
	def test_meta_completo_nao_avisa(self):
		self.assertEqual(self._rodar(), [])
