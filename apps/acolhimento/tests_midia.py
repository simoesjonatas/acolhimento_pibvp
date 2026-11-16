"""Testes da midia recebida pelo WhatsApp (API oficial da Meta). Sem rede.

Cobrem a extracao dos ids no webhook, o download (mockado) com limite de tamanho e
retentativa, a view autenticada que entrega o arquivo e a renderizacao na conversa.
"""
import json
import os
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import meta_service, midia_whatsapp
from apps.acolhimento.models import MensagemContato, PrimeiroContato
from apps.acolhimento.tests_meta import META_OK, SIMPLE_STATIC_STORAGES, _http_error, _pessoa

Status = MensagemContato.MidiaStatusChoices


class _RespostaBinaria:
	def __init__(self, conteudo: bytes, *, status=200, content_length=None):
		self.status = status
		self._conteudo = conteudo
		self.headers = {} if content_length is None else {'Content-Length': str(content_length)}

	def read(self, n=-1):
		return self._conteudo if n is None or n < 0 else self._conteudo[:n]

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False


class _RespostaJson(_RespostaBinaria):
	def __init__(self, body):
		super().__init__(json.dumps(body).encode('utf-8'))


class _PastaMidiaTemporaria:
	"""Mixin: MIDIA_PRIVADA_ROOT numa pasta temporaria, apagada no fim do teste."""

	def setUp(self):
		super().setUp()
		self.pasta_midia = tempfile.mkdtemp(prefix='midia-teste-')
		self.addCleanup(shutil.rmtree, self.pasta_midia, ignore_errors=True)
		override = override_settings(MIDIA_PRIVADA_ROOT=self.pasta_midia)
		override.enable()
		self.addCleanup(override.disable)


def _entrada_midia(pessoa, tipo='audio', status=Status.PENDENTE, **extra):
	dados = dict(
		pessoa=pessoa,
		canal=MensagemContato.CanalChoices.WHATSAPP,
		direcao=MensagemContato.DirecaoChoices.ENTRADA,
		status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
		conteudo='(audio)' if tipo == 'audio' else '(documento)',
		midia_tipo=tipo,
		midia_id='MEDIA123',
		midia_status=status,
		midia_mime='audio/ogg; codecs=opus' if tipo == 'audio' else 'application/pdf',
	)
	dados.update(extra)
	return MensagemContato.objects.create(**dados)


# ---------------------------------------------------------------------------
# Extracao dos ids
# ---------------------------------------------------------------------------
class ExtrairMidiaTests(TestCase):
	def test_extrai_audio_imagem_video_documento(self):
		casos = {
			'audio': {'id': 'A1', 'mime_type': 'audio/ogg; codecs=opus', 'sha256': 'h', 'voice': True},
			'image': {'id': 'I1', 'mime_type': 'image/jpeg', 'caption': 'olha'},
			'video': {'id': 'V1', 'mime_type': 'video/mp4'},
			'document': {'id': 'D1', 'mime_type': 'application/pdf', 'filename': 'boletim.pdf'},
		}
		for tipo, bloco in casos.items():
			with self.subTest(tipo=tipo):
				midia = midia_whatsapp.extrair_midia({'type': tipo, tipo: bloco})
				self.assertEqual(midia['tipo'], tipo)
				self.assertEqual(midia['media_id'], bloco['id'])
				self.assertEqual(midia['mime_type'], bloco['mime_type'])
		self.assertEqual(
			midia_whatsapp.extrair_midia({'type': 'document', 'document': casos['document']})['filename'],
			'boletim.pdf',
		)

	def test_sem_midia_devolve_none(self):
		self.assertIsNone(midia_whatsapp.extrair_midia({'type': 'text', 'text': {'body': 'oi'}}))
		self.assertIsNone(midia_whatsapp.extrair_midia({'type': 'audio', 'audio': {}}))
		self.assertEqual(midia_whatsapp.campos_midia_entrada({'type': 'location'}), {})


# ---------------------------------------------------------------------------
# meta_service: URL e download
# ---------------------------------------------------------------------------
@override_settings(**META_OK)
class MetaServiceMidiaTests(TestCase):
	def test_obter_url_midia_consulta_o_id_com_token(self):
		capturado = {}

		def fake_urlopen(req, timeout=None):
			capturado['url'] = req.full_url
			capturado['auth'] = req.get_header('Authorization')
			return _RespostaJson({'url': 'https://lookaside.fbsbx.com/x', 'mime_type': 'audio/ogg', 'file_size': 10})

		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', side_effect=fake_urlopen):
			info = meta_service.obter_url_midia('MEDIA123')

		self.assertTrue(capturado['url'].endswith('/v22.0/MEDIA123'))
		self.assertEqual(capturado['auth'], 'Bearer token-teste')
		self.assertEqual(info['url'], 'https://lookaside.fbsbx.com/x')

	def test_obter_url_midia_sem_url_e_erro(self):
		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', return_value=_RespostaJson({'id': 'x'})):
			with self.assertRaises(meta_service.MetaWhatsAppError):
				meta_service.obter_url_midia('MEDIA123')

	def test_baixar_midia_usa_bearer_e_devolve_bytes(self):
		capturado = {}

		def fake_urlopen(req, timeout=None):
			capturado['auth'] = req.get_header('Authorization')
			return _RespostaBinaria(b'OggS-audio')

		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', side_effect=fake_urlopen):
			conteudo = meta_service.baixar_midia('https://lookaside.fbsbx.com/x')

		self.assertEqual(conteudo, b'OggS-audio')
		self.assertEqual(capturado['auth'], 'Bearer token-teste')

	def test_baixar_midia_recusa_acima_do_limite(self):
		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', return_value=_RespostaBinaria(b'x' * 11)):
			with self.assertRaises(meta_service.MidiaGrandeDemaisError):
				meta_service.baixar_midia('https://lookaside.fbsbx.com/x', limite_bytes=10)
		resposta = _RespostaBinaria(b'', content_length=10_000)
		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', return_value=resposta):
			with self.assertRaises(meta_service.MidiaGrandeDemaisError):
				meta_service.baixar_midia('https://lookaside.fbsbx.com/x', limite_bytes=10)

	def test_baixar_midia_http_erro_e_url_invalida(self):
		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', side_effect=_http_error(404, {})):
			with self.assertRaises(meta_service.MetaWhatsAppError):
				meta_service.baixar_midia('https://lookaside.fbsbx.com/x')
		with self.assertRaises(meta_service.MetaWhatsAppError):
			meta_service.baixar_midia('http://inseguro/x')

	@override_settings(META_WA_MIDIA_MAX_MB=3)
	def test_limite_vem_do_settings(self):
		self.assertEqual(meta_service.limite_midia_bytes(), 3 * 1024 * 1024)


# ---------------------------------------------------------------------------
# Download da mensagem (com retentativa)
# ---------------------------------------------------------------------------
@override_settings(**META_OK)
class DownloadMidiaTests(_PastaMidiaTemporaria, TestCase):
	def setUp(self):
		super().setUp()
		self.pessoa = _pessoa()

	def _mock_meta(self, *, info=None, conteudo=b'OggS-audio', erro=None):
		info = info or {'url': 'https://lookaside.fbsbx.com/x', 'mime_type': 'audio/ogg; codecs=opus', 'file_size': 10}
		obter = patch('apps.acolhimento.meta_service.obter_url_midia', return_value=info, side_effect=erro)
		baixar = patch('apps.acolhimento.meta_service.baixar_midia', return_value=conteudo)
		return obter, baixar

	def test_baixa_e_salva_fora_da_pasta_publica(self):
		mensagem = _entrada_midia(self.pessoa)
		obter, baixar = self._mock_meta()
		with obter as mock_obter, baixar:
			self.assertTrue(midia_whatsapp.baixar_midia_da_mensagem(mensagem.pk))

		mock_obter.assert_called_once_with('MEDIA123')
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.midia_status, Status.BAIXADA)
		self.assertEqual(mensagem.midia_tamanho, 10)
		self.assertTrue(mensagem.midia_arquivo.name.endswith('.ogg'))
		self.assertTrue(mensagem.midia_arquivo.path.startswith(self.pasta_midia))
		with mensagem.midia_arquivo.open('rb') as arquivo:
			self.assertEqual(arquivo.read(), b'OggS-audio')
		# Sem URL publica: so pela view autenticada.
		with self.assertRaises(ValueError):
			mensagem.midia_arquivo.url

	def test_falha_de_rede_mantem_pendente_e_conta_tentativa(self):
		mensagem = _entrada_midia(self.pessoa)
		obter, baixar = self._mock_meta(erro=meta_service.MetaWhatsAppError('Meta fora do ar'))
		with obter, baixar as mock_baixar:
			self.assertFalse(midia_whatsapp.baixar_midia_da_mensagem(mensagem.pk))

		mock_baixar.assert_not_called()
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.midia_status, Status.PENDENTE)
		self.assertEqual(mensagem.midia_tentativas, 1)
		self.assertIn('Meta fora do ar', mensagem.midia_erro)
		# A mensagem de texto continua la, intacta.
		self.assertEqual(mensagem.conteudo, '(audio)')

	def test_desiste_apos_maximo_de_tentativas(self):
		mensagem = _entrada_midia(self.pessoa, midia_tentativas=midia_whatsapp.MAX_TENTATIVAS - 1)
		obter, baixar = self._mock_meta(erro=RuntimeError('timeout'))
		with obter, baixar:
			midia_whatsapp.baixar_midia_da_mensagem(mensagem.pk)
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.midia_status, Status.FALHA)

	@override_settings(META_WA_MIDIA_MAX_MB=1)
	def test_maior_que_o_limite_falha_sem_baixar(self):
		mensagem = _entrada_midia(self.pessoa)
		info = {'url': 'https://lookaside.fbsbx.com/x', 'mime_type': 'video/mp4', 'file_size': 5 * 1024 * 1024}
		obter, baixar = self._mock_meta(info=info)
		with obter, baixar as mock_baixar:
			self.assertFalse(midia_whatsapp.baixar_midia_da_mensagem(mensagem.pk))

		mock_baixar.assert_not_called()
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.midia_status, Status.FALHA)
		self.assertIn('limite', mensagem.midia_erro)

	def test_cron_retenta_pendentes_antigas_e_expira_as_vencidas(self):
		antiga = _entrada_midia(self.pessoa)
		recente = _entrada_midia(self.pessoa, midia_id='MEDIA_RECENTE')
		vencida = _entrada_midia(self.pessoa, midia_id='MEDIA_VELHA')
		agora = timezone.now()
		MensagemContato.objects.filter(pk=antiga.pk).update(enfileirada_em=agora - timedelta(minutes=10))
		MensagemContato.objects.filter(pk=vencida.pk).update(enfileirada_em=agora - timedelta(days=40))

		obter, baixar = self._mock_meta()
		with obter as mock_obter, baixar:
			self.assertEqual(midia_whatsapp.baixar_midias_pendentes(), 1)

		mock_obter.assert_called_once_with('MEDIA123')
		self.assertEqual(MensagemContato.objects.get(pk=antiga.pk).midia_status, Status.BAIXADA)
		self.assertEqual(MensagemContato.objects.get(pk=recente.pk).midia_status, Status.PENDENTE)
		self.assertEqual(MensagemContato.objects.get(pk=vencida.pk).midia_status, Status.FALHA)

	def test_thread_tenta_de_novo_ate_conseguir(self):
		mensagem = _entrada_midia(self.pessoa)
		info = {'url': 'https://lookaside.fbsbx.com/x', 'mime_type': 'audio/ogg', 'file_size': 10}
		obter = patch(
			'apps.acolhimento.meta_service.obter_url_midia',
			side_effect=[meta_service.MetaWhatsAppError('instavel'), info],
		)
		with obter, patch('apps.acolhimento.meta_service.baixar_midia', return_value=b'OggS'), \
				patch('apps.acolhimento.midia_whatsapp.time.sleep') as dormir:
			midia_whatsapp._baixar_com_retentativa(mensagem.pk)

		dormir.assert_called_once()
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.midia_status, Status.BAIXADA)
		self.assertEqual(mensagem.midia_tentativas, 2)


# ---------------------------------------------------------------------------
# Webhook: grava o id e agenda o download sem bloquear a resposta
# ---------------------------------------------------------------------------
@override_settings(**{**META_OK, 'META_WA_APP_SECRET': ''}, DEBUG=True)
class WebhookMidiaTests(TestCase):
	def setUp(self):
		patcher = patch('apps.acolhimento.meta_service.marcar_como_lida', return_value=True)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _postar(self, message):
		payload = {
			'object': 'whatsapp_business_account',
			'entry': [{'changes': [{'field': 'messages', 'value': {
				'contacts': [{'profile': {'name': 'Ana'}, 'wa_id': '5521971347985'}],
				'messages': [message],
			}}]}],
		}
		# Sem App Secret e com DEBUG o webhook aceita sem assinatura (so em teste/dev).
		return self.client.post(
			reverse('mensagens-webhook-meta'), data=json.dumps(payload), content_type='application/json',
		)

	def test_audio_grava_id_pendente_e_agenda_download_apos_commit(self):
		message = {
			'from': '5521971347985', 'id': 'wamid.AUDIO1', 'timestamp': '1700000000', 'type': 'audio',
			'audio': {'id': 'MEDIA_AUDIO', 'mime_type': 'audio/ogg; codecs=opus', 'sha256': 'h', 'voice': True},
		}
		# Nenhuma chamada de rede durante o webhook: o download so roda no on_commit.
		with patch('apps.acolhimento.meta_service.obter_url_midia') as obter, \
				patch('apps.acolhimento.midia_whatsapp.threading.Thread') as thread, \
				self.captureOnCommitCallbacks(execute=False) as callbacks:
			resp = self._postar(message)
			self.assertEqual(resp.status_code, 200)
			obter.assert_not_called()
			thread.assert_not_called()

		mensagem = MensagemContato.objects.get(referencia_externa='wamid.AUDIO1')
		self.assertEqual(mensagem.conteudo, '(audio)')
		self.assertEqual(mensagem.midia_tipo, 'audio')
		self.assertEqual(mensagem.midia_id, 'MEDIA_AUDIO')
		self.assertEqual(mensagem.midia_status, Status.PENDENTE)
		self.assertEqual(mensagem.legenda_midia, '')

		with patch('apps.acolhimento.midia_whatsapp.threading.Thread') as thread:
			for callback in callbacks:
				callback()
		thread.assert_called_once()
		self.assertEqual(thread.call_args.kwargs['args'], (mensagem.pk,))
		thread.return_value.start.assert_called_once()

	def test_imagem_com_legenda_e_texto_sem_midia(self):
		with patch('apps.acolhimento.midia_whatsapp.agendar_download') as agendar:
			self._postar({
				'from': '5521971347985', 'id': 'wamid.IMG1', 'type': 'image',
				'image': {'id': 'MEDIA_IMG', 'mime_type': 'image/jpeg', 'caption': 'Foto do culto'},
			})
			self._postar({'from': '5521971347985', 'id': 'wamid.TXT1', 'type': 'text', 'text': {'body': 'oi'}})

		imagem = MensagemContato.objects.get(referencia_externa='wamid.IMG1')
		self.assertEqual(imagem.midia_tipo, 'image')
		self.assertEqual(imagem.legenda_midia, 'Foto do culto')
		texto = MensagemContato.objects.get(referencia_externa='wamid.TXT1')
		self.assertEqual(texto.midia_tipo, '')
		self.assertEqual(texto.midia_status, '')
		agendar.assert_called_once_with(imagem.pk)


# ---------------------------------------------------------------------------
# View autenticada + tela da conversa
# ---------------------------------------------------------------------------
@override_settings(**META_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class MidiaViewTests(_PastaMidiaTemporaria, TestCase):
	def setUp(self):
		super().setUp()
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.pessoa = _pessoa()
		self.audio = _entrada_midia(self.pessoa, status=Status.BAIXADA, midia_tamanho=10)
		self.audio.midia_arquivo.save('midia.ogg', ContentFile(b'0123456789'), save=True)
		self.url = reverse('mensagens-midia', args=[self.audio.pk])

	def _usuario(self, nome, com_permissao):
		user = get_user_model().objects.create_user(nome, password='x')
		if com_permissao:
			user.user_permissions.add(Permission.objects.get(codename='pode_conversar_pessoas'))
			# Usuario comum so ve os proprios acolhidos.
			PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(responsavel_atual=user)
		return user

	def test_anonimo_bloqueado(self):
		# Mesmo comportamento das outras telas da conversa (raise_exception => 403).
		resp = self.client.get(self.url)
		self.assertEqual(resp.status_code, 403)
		self.assertNotIn(b'0123456789', resp.content)

	def test_sem_permissao_403(self):
		self.client.force_login(self._usuario('sem_perm', False))
		self.assertEqual(self.client.get(self.url).status_code, 403)

	def test_com_permissao_recebe_o_arquivo(self):
		self.client.force_login(self._usuario('com_perm', True))
		resp = self.client.get(self.url)
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(b''.join(resp.streaming_content), b'0123456789')
		self.assertEqual(resp['Content-Type'], 'audio/ogg')
		self.assertIn('inline', resp['Content-Disposition'])
		self.assertIn('private', resp['Cache-Control'])
		self.assertIn('sandbox', resp['Content-Security-Policy'])

	def test_range_devolve_206_para_o_safari(self):
		self.client.force_login(self._usuario('com_perm', True))
		resp = self.client.get(self.url, HTTP_RANGE='bytes=2-5')
		self.assertEqual(resp.status_code, 206)
		self.assertEqual(b''.join(resp.streaming_content), b'2345')
		self.assertEqual(resp['Content-Range'], 'bytes 2-5/10')
		resp = self.client.get(self.url, HTTP_RANGE='bytes=50-')
		self.assertEqual(resp.status_code, 416)

	def test_documento_sai_como_download_e_mime_perigoso_vira_binario(self):
		doc = _entrada_midia(
			self.pessoa, tipo='document', status=Status.BAIXADA,
			midia_mime='image/svg+xml', midia_nome_original='desenho.svg',
		)
		doc.midia_arquivo.save('midia.svg', ContentFile(b'<svg/>'), save=True)
		self.client.force_login(self._usuario('com_perm', True))
		resp = self.client.get(reverse('mensagens-midia', args=[doc.pk]))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp['Content-Type'], 'application/octet-stream')
		self.assertIn('attachment', resp['Content-Disposition'])
		self.assertIn('desenho.svg', resp['Content-Disposition'])

	def test_midia_nao_baixada_404(self):
		pendente = _entrada_midia(self.pessoa)
		self.client.force_login(self._usuario('com_perm', True))
		self.assertEqual(self.client.get(reverse('mensagens-midia', args=[pendente.pk])).status_code, 404)

	def test_conversa_mostra_player_miniatura_e_fallback(self):
		imagem = _entrada_midia(
			self.pessoa, tipo='image', status=Status.BAIXADA, midia_mime='image/jpeg', conteudo='Foto do culto',
		)
		imagem.midia_arquivo.save('midia.jpg', ContentFile(b'\xff\xd8'), save=True)
		_entrada_midia(self.pessoa, midia_id='MEDIA_PEND', status=Status.FALHA)
		self.client.force_login(self._usuario('com_perm', True))

		resp = self.client.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))

		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, f'<audio controls preload="metadata" src="{self.url}">', html=False)
		self.assertContains(resp, f'<img src="{reverse("mensagens-midia", args=[imagem.pk])}"', html=False)
		self.assertContains(resp, 'Foto do culto')
		self.assertContains(resp, '(áudio indisponível)')
		# O marcador "(audio)" nao aparece como texto quando ha player.
		self.assertNotContains(resp, '<p>(audio)</p>', html=False)
		self.assertNotIn(self.pasta_midia, resp.content.decode())

	def test_excluir_pessoa_apaga_o_arquivo_do_disco(self):
		caminho = self.audio.midia_arquivo.path
		self.assertTrue(os.path.exists(caminho))
		with self.captureOnCommitCallbacks(execute=True):
			PrimeiroContato.objects.filter(pk=self.pessoa.pk).delete()
		self.assertFalse(os.path.exists(caminho))
		self.client.force_login(self._usuario('com_perm', True))
		self.assertEqual(self.client.get(self.url).status_code, 404)
