"""Templates da Meta com midia no cabecalho (video, imagem ou PDF) no disparo em massa.

Caso real que motivou: o template `relig_conference` foi aprovado com video no
cabecalho e {{1}} no corpo; o disparo mandava sem nenhum parametro e a Meta recusava
cada envio (#132000). Agora o disparo sobe a midia uma vez, grava o id em cada
mensagem e confere o template na Meta antes de enfileirar. Nenhum teste toca a rede.
"""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import meta_service, whatsapp_gateway
from apps.acolhimento.models import MensagemContato, PrimeiroContato
from apps.acolhimento.tests_meta import META_OK, SIMPLE_STATIC_STORAGES, _RespostaFake, _pessoa, _saida

# Igual ao `relig_conference` aprovado na Meta (video no cabecalho + {{1}} no corpo).
TEMPLATE_COM_VIDEO = {
	'name': 'relig_conference',
	'language': 'pt_BR',
	'status': 'APPROVED',
	'category': 'UTILITY',
	'components': [
		{'type': 'HEADER', 'format': 'VIDEO', 'example': {'header_handle': ['https://exemplo/video.mp4']}},
		{'type': 'BODY', 'text': 'Olá, {{1}}! Graça e Paz! Vimos que você esteve conosco.'},
	],
}


def _video(nome='convite.mp4', tamanho=1024):
	return SimpleUploadedFile(nome, b'\x00' * tamanho, content_type='video/mp4')


# ---------------------------------------------------------------------------
# meta_service
# ---------------------------------------------------------------------------
@override_settings(**META_OK)
class CabecalhoMetaServiceTests(TestCase):
	def test_video_entra_no_header_antes_do_corpo(self):
		componentes = meta_service.montar_componentes(
			{'1': 'Maria'},
			{'tipo': 'video', 'id': 'media-1', 'nome': 'convite.mp4'},
		)
		self.assertEqual(componentes[0], {
			'type': 'header',
			'parameters': [{'type': 'video', 'video': {'id': 'media-1'}}],
		})
		self.assertEqual(componentes[1]['type'], 'body')
		self.assertEqual(componentes[1]['parameters'], [{'type': 'text', 'text': 'Maria'}])

	def test_so_cabecalho_sem_variaveis(self):
		componentes = meta_service.montar_componentes({}, {'tipo': 'image', 'id': 'media-2'})
		self.assertEqual(len(componentes), 1)
		self.assertEqual(componentes[0]['parameters'][0], {'type': 'image', 'image': {'id': 'media-2'}})

	def test_documento_leva_nome_do_arquivo(self):
		componentes = meta_service.montar_componentes(None, {'tipo': 'document', 'id': 'media-3', 'nome': 'programa.pdf'})
		self.assertEqual(componentes[0]['parameters'][0]['document'], {'id': 'media-3', 'filename': 'programa.pdf'})

	def test_cabecalho_sem_id_ou_tipo_invalido_falha(self):
		for cabecalho in ({'tipo': 'video'}, {'tipo': 'audio', 'id': 'x'}):
			with self.subTest(cabecalho=cabecalho), self.assertRaises(meta_service.MetaWhatsAppError):
				meta_service.montar_componentes({}, cabecalho)

	def test_send_template_manda_o_video_no_payload(self):
		capturado = {}

		def fake_urlopen(req, timeout=None):
			capturado['body'] = json.loads(req.data.decode('utf-8'))
			return _RespostaFake(200, {'messages': [{'id': 'wamid.V'}]})

		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', side_effect=fake_urlopen):
			meta_service.send_template(
				to_phone='5521971347985',
				nome='relig_conference',
				parametros={'1': 'Maria'},
				cabecalho={'tipo': 'video', 'id': 'media-1'},
			)

		componentes = capturado['body']['template']['components']
		self.assertEqual([c['type'] for c in componentes], ['header', 'body'])
		self.assertEqual(componentes[0]['parameters'][0]['video'], {'id': 'media-1'})

	def test_requisitos_do_template_com_video(self):
		requisitos = meta_service.requisitos_template(TEMPLATE_COM_VIDEO)
		self.assertEqual(requisitos['cabecalho'], 'VIDEO')
		self.assertEqual(requisitos['cabecalho_midia'], 'video')
		self.assertEqual(requisitos['corpo_posicionais'], 1)
		self.assertEqual(requisitos['corpo_nomeados'], [])
		self.assertEqual(requisitos['variaveis_nao_suportadas'], [])
		self.assertEqual(requisitos['status'], 'APPROVED')

	def test_requisitos_marcam_variavel_em_botao_e_cabecalho_de_texto(self):
		requisitos = meta_service.requisitos_template({'components': [
			{'type': 'HEADER', 'format': 'TEXT', 'text': 'Oi {{1}}'},
			{'type': 'BODY', 'text': 'Ola {{nome}}, tudo bem? {{nome}}'},
			{'type': 'BUTTONS', 'buttons': [{'type': 'URL', 'url': 'https://x.com/{{1}}'}]},
		]})
		self.assertEqual(requisitos['cabecalho_midia'], '')
		self.assertEqual(requisitos['corpo_nomeados'], ['nome'])
		self.assertEqual(requisitos['variaveis_nao_suportadas'], ['cabecalho', 'botao'])

	def test_obter_template_confere_nome_exato_e_idioma(self):
		resposta = {'data': [
			{'name': 'relig_conference_2', 'language': 'pt_BR'},
			{'name': 'relig_conference', 'language': 'en_US'},
			TEMPLATE_COM_VIDEO,
		]}
		with patch('apps.acolhimento.meta_service._request', return_value=(200, resposta)) as request:
			self.assertEqual(meta_service.obter_template('relig_conference', 'pt_BR'), TEMPLATE_COM_VIDEO)
			self.assertEqual(meta_service.obter_template('relig_conference', 'es_ES'), {})
		self.assertEqual(request.call_args.kwargs['params']['name'], 'relig_conference')

	def test_obter_template_sem_consulta_devolve_none(self):
		with patch('apps.acolhimento.meta_service._request', side_effect=meta_service.MetaWhatsAppError('rede')):
			self.assertIsNone(meta_service.obter_template('relig_conference', 'pt_BR'))
		with override_settings(META_WA_BUSINESS_ACCOUNT_ID=''):
			self.assertIsNone(meta_service.obter_template('relig_conference', 'pt_BR'))


@override_settings(**META_OK)
class CabecalhoGatewayTests(TestCase):
	def test_gateway_repassa_o_cabecalho_gravado_na_mensagem(self):
		cabecalho = {'tipo': 'video', 'id': 'media-1', 'nome': 'convite.mp4'}
		msg = _saida(_pessoa(), metadata_envio={
			'tipo_template': 'marketing_massa',
			'meta_template': {'nome': 'relig_conference', 'idioma': 'pt_BR', 'parametros': {'1': 'Maria'}, 'cabecalho': cabecalho},
		})
		with patch('apps.acolhimento.meta_service.send_template') as send_template:
			send_template.return_value = {'sid': 'wamid.1', 'status': 'accepted', 'to': 'x', 'raw': {}}
			whatsapp_gateway.enviar(msg, '+5521971347985')

		send_template.assert_called_once_with(
			to_phone='+5521971347985',
			nome='relig_conference',
			idioma='pt_BR',
			parametros={'1': 'Maria'},
			cabecalho=cabecalho,
		)


# ---------------------------------------------------------------------------
# Tela de disparo em massa
# ---------------------------------------------------------------------------
@override_settings(**META_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class DisparoTemplateComCabecalhoTests(TestCase):
	def setUp(self):
		# O indicador do WhatsApp no menu consulta a Meta ao renderizar qualquer tela.
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.user = get_user_model().objects.create_user('equipe_disparo', password='x', is_staff=True)
		self.client.force_login(self.user)
		participante = PrimeiroContato.StatusAcolhimento.PARTICIPANTE
		self.ana = _pessoa(nome='Ana', telefone='31955554444', status=participante)
		self.bia = _pessoa(nome='Bia', telefone='31955553333', status=participante)

	def _postar(self, *, template=TEMPLATE_COM_VIDEO, arquivo=None, variaveis='{"1": "{nome}"}', media_id='media-123'):
		dados = {
			'modo': 'template',
			'content_sid': 'relig_conference',
			'template_idioma': 'pt_BR',
			'content_variables': variaveis,
			'pessoas': [self.ana.pk, self.bia.pk],
		}
		if arquivo is not None:
			dados['cabecalho_midia'] = arquivo
		with patch('apps.acolhimento.meta_service.obter_template', return_value=template), patch(
			'apps.acolhimento.meta_service.enviar_arquivo_para_meta', return_value=media_id,
		) as upload:
			resp = self.client.post(reverse('mensagens-disparo-massa'), dados)
		return resp, upload

	def test_video_sobe_uma_vez_e_vai_em_todas_as_mensagens(self):
		resp, upload = self._postar(arquivo=_video())

		self.assertEqual(resp.status_code, 302)
		upload.assert_called_once()
		self.assertEqual(upload.call_args.kwargs['mime'], 'video/mp4')
		self.assertEqual(upload.call_args.kwargs['nome'], 'convite.mp4')
		mensagens = MensagemContato.objects.order_by('pessoa__nome')
		self.assertEqual(mensagens.count(), 2)
		for mensagem, nome in zip(mensagens, ['Ana', 'Bia']):
			meta_template = mensagem.metadata_envio['meta_template']
			self.assertEqual(meta_template['parametros'], {'1': nome})
			self.assertEqual(meta_template['cabecalho']['tipo'], 'video')
			self.assertEqual(meta_template['cabecalho']['id'], 'media-123')
			self.assertEqual(meta_template['cabecalho']['nome'], 'convite.mp4')

	def test_template_com_video_sem_arquivo_nao_enfileira(self):
		resp, upload = self._postar()

		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'tem um video no cabecalho: anexe o arquivo')
		upload.assert_not_called()
		self.assertFalse(MensagemContato.objects.exists())

	def test_variavel_do_corpo_faltando_nao_enfileira(self):
		"""O erro real: {{1}} no corpo e o disparo saia sem nenhum parametro (#132000)."""
		resp, upload = self._postar(arquivo=_video(), variaveis='')

		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'tem 1 variavel(is)')
		upload.assert_not_called()
		self.assertFalse(MensagemContato.objects.exists())

	def test_arquivo_de_tipo_diferente_do_cabecalho(self):
		imagem = SimpleUploadedFile('cartaz.jpg', b'\xff\xd8\xff', content_type='image/jpeg')
		resp, upload = self._postar(arquivo=imagem)

		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'e um video, mas o arquivo enviado e uma imagem')
		upload.assert_not_called()
		self.assertFalse(MensagemContato.objects.exists())

	def test_formato_nao_aceito_no_cabecalho(self):
		for nome in ('audio.mp3', 'planilha.xlsx'):
			with self.subTest(nome=nome):
				resp, upload = self._postar(arquivo=SimpleUploadedFile(nome, b'x'))
				self.assertEqual(resp.status_code, 200)
				self.assertContains(resp, 'Formato nao aceito no cabecalho')
				upload.assert_not_called()
		self.assertFalse(MensagemContato.objects.exists())

	def test_arquivo_acima_do_limite(self):
		with patch('apps.acolhimento.bot_midia.LIMITE_MB', {'image': 5, 'video': 0, 'audio': 16, 'document': 25}):
			resp, upload = self._postar(arquivo=_video())
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'passa do limite')
		upload.assert_not_called()

	def test_template_sem_midia_recusa_arquivo(self):
		so_texto = {**TEMPLATE_COM_VIDEO, 'components': [TEMPLATE_COM_VIDEO['components'][1]]}
		resp, upload = self._postar(template=so_texto, arquivo=_video())

		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'nao tem midia no cabecalho')
		upload.assert_not_called()

	def test_template_inexistente(self):
		resp, _upload = self._postar(template={}, arquivo=_video())
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'nao encontrado na Meta')
		self.assertFalse(MensagemContato.objects.exists())

	def test_template_nao_aprovado(self):
		resp, _upload = self._postar(template={**TEMPLATE_COM_VIDEO, 'status': 'PAUSED'}, arquivo=_video())
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'status PAUSED')
		self.assertFalse(MensagemContato.objects.exists())

	def test_sem_consulta_a_meta_confia_no_arquivo(self):
		"""Se a Meta nao responder a conferencia, o disparo segue com o que foi informado."""
		resp, upload = self._postar(template=None, arquivo=_video())

		self.assertEqual(resp.status_code, 302)
		upload.assert_called_once()
		self.assertEqual(MensagemContato.objects.count(), 2)

	def test_falha_no_upload_mostra_erro_e_nao_enfileira(self):
		dados = {
			'modo': 'template',
			'content_sid': 'relig_conference',
			'template_idioma': 'pt_BR',
			'content_variables': '{"1": "{nome}"}',
			'pessoas': [self.ana.pk],
			'cabecalho_midia': _video(),
		}
		with patch('apps.acolhimento.meta_service.obter_template', return_value=TEMPLATE_COM_VIDEO), patch(
			'apps.acolhimento.meta_service.enviar_arquivo_para_meta',
			side_effect=meta_service.MetaWhatsAppError('Arquivo recusado'),
		):
			resp = self.client.post(reverse('mensagens-disparo-massa'), dados)

		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Nao foi possivel enviar o arquivo para a Meta: Arquivo recusado')
		self.assertFalse(MensagemContato.objects.exists())

	def test_template_sem_variaveis_recusa_variaveis_sobrando(self):
		sem_vars = {**TEMPLATE_COM_VIDEO, 'components': [
			TEMPLATE_COM_VIDEO['components'][0],
			{'type': 'BODY', 'text': 'Graça e Paz!'},
		]}
		resp, _upload = self._postar(template=sem_vars, arquivo=_video())
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'nao tem variaveis no texto')
