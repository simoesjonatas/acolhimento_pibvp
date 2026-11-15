"""Transcricao de audios: Groq primeiro, faster-whisper local quando a Groq da 429. Sem rede."""
import io
import json
import shutil
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import atendimento_ia, ia_service, midia_whatsapp, transcricao, transcricao_local
from apps.acolhimento.models import (
	ConfiguracaoAtendimentoBot,
	MensagemContato,
	OpcaoAtendimentoBot,
	PrimeiroContato,
)
from apps.acolhimento.tests_meta import META_OK, _http_error, _pessoa

Status = MensagemContato.TranscricaoStatusChoices
GROQ_OK = dict(GROQ_API_KEY='groq-teste', GROQ_BASE_URL='https://groq.teste/v1', TRANSCRICAO_LOCAL_ATIVA=True)


class _Resposta:
	def __init__(self, body):
		self._corpo = json.dumps(body).encode('utf-8')

	def read(self, n=-1):
		return self._corpo

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False


class _Base(TestCase):
	def setUp(self):
		self.pasta = tempfile.mkdtemp(prefix='transcricao-teste-')
		self.addCleanup(shutil.rmtree, self.pasta, ignore_errors=True)
		override = override_settings(MIDIA_PRIVADA_ROOT=self.pasta, WHISPER_MODELOS_DIR=self.pasta, **GROQ_OK)
		override.enable()
		self.addCleanup(override.disable)

		OpcaoAtendimentoBot.objects.all().delete()
		OpcaoAtendimentoBot.objects.create(
			ordem=1, rotulo='Horario dos cultos', palavras_chave='horario', resposta='Domingo as 9h e 18h.',
		)
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.transcrever_audios = True
		cfg.save()
		self.pessoa = _pessoa(
			nome='Bia', telefone='21970002222',
			status=PrimeiroContato.StatusAcolhimento.ROBO, bot_etapa=PrimeiroContato.BotEtapaChoices.MENU,
		)

	def _audio(self, aguardando=True, **extra):
		mensagem = MensagemContato(
			pessoa=self.pessoa, canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA, status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo='(audio)', midia_tipo='audio', midia_id='M1', midia_mime='audio/ogg; codecs=opus',
			midia_status=MensagemContato.MidiaStatusChoices.BAIXADA,
			**{
				'transcricao_status': Status.PENDENTE,
				'metadata_resposta': {'bot_aguardando_transcricao': True} if aguardando else {},
				**extra,
			},
		)
		mensagem.midia_arquivo.save('midia.ogg', ContentFile(b'OggS-fake'), save=False)
		mensagem.save()
		return mensagem

	def _saidas(self):
		return list(MensagemContato.objects.filter(
			pessoa=self.pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA,
		).order_by('id'))


class MotoresTests(_Base):
	def test_groq_transcreve(self):
		mensagem = self._audio()
		with patch.object(ia_service, 'transcrever_groq', return_value='quero saber o horario') as groq, \
				patch.object(transcricao_local, 'transcrever') as local:
			self.assertTrue(transcricao.transcrever_mensagem(mensagem.pk))
		local.assert_not_called()
		self.assertEqual(groq.call_args.args[0], b'OggS-fake')
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.transcricao, 'quero saber o horario')
		self.assertEqual(mensagem.transcricao_status, Status.OK)
		self.assertEqual(mensagem.metadata_resposta['transcricao']['motor'], 'groq')

	def test_groq_429_ou_fora_do_ar_usa_o_servidor(self):
		for erro in (ia_service.IAError('HTTP 429', http_status=429),
					 ia_service.IAError('HTTP 503', http_status=503),
					 ia_service.IAError('sem conexao')):
			with self.subTest(status=erro.http_status):
				mensagem = self._audio()
				with patch.object(ia_service, 'transcrever_groq', side_effect=erro), \
						patch.object(transcricao_local, 'transcrever',
									 return_value={'texto': 'oi tudo bem', 'modelo': 'small', 'duracao': 3.2}) as local:
					self.assertTrue(transcricao.transcrever_mensagem(mensagem.pk))
				self.assertEqual(local.call_args.args[0], mensagem.midia_arquivo.path)
				mensagem.refresh_from_db()
				self.assertEqual(mensagem.transcricao, 'oi tudo bem')
				self.assertEqual(mensagem.metadata_resposta['transcricao']['motor'], 'local')

	def test_groq_400_nao_usa_a_reserva(self):
		mensagem = self._audio()
		with patch.object(ia_service, 'transcrever_groq', side_effect=ia_service.IAError('HTTP 400', http_status=400)), \
				patch.object(transcricao_local, 'transcrever') as local:
			self.assertFalse(transcricao.transcrever_mensagem(mensagem.pk))
		local.assert_not_called()
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.transcricao_status, Status.FALHA)

	@override_settings(GROQ_API_KEY='')
	def test_sem_groq_vai_direto_para_o_servidor(self):
		mensagem = self._audio()
		with patch.object(ia_service, 'transcrever_groq') as groq, \
				patch.object(transcricao_local, 'transcrever', return_value={'texto': 'oi', 'modelo': 'small', 'duracao': 1}):
			self.assertTrue(transcricao.transcrever_mensagem(mensagem.pk))
		groq.assert_not_called()

	def test_reserva_indisponivel_registra_falha(self):
		mensagem = self._audio()
		with patch.object(ia_service, 'transcrever_groq', side_effect=ia_service.IAError('HTTP 429', http_status=429)), \
				patch.object(transcricao_local, 'transcrever',
							 side_effect=transcricao_local.TranscricaoLocalIndisponivel('faster-whisper nao esta instalado')):
			self.assertFalse(transcricao.transcrever_mensagem(mensagem.pk))
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.transcricao_status, Status.FALHA)
		self.assertIn('HTTP 429', mensagem.metadata_resposta['transcricao']['erro'])
		self.assertIn('faster-whisper', mensagem.metadata_resposta['transcricao']['erro'])


class RoboDepoisDaTranscricaoTests(_Base):
	def test_audio_transcrito_e_respondido_pelo_menu(self):
		mensagem = self._audio()
		with patch.object(ia_service, 'transcrever_groq', return_value='qual o horario do culto'):
			transcricao.depois_do_download(mensagem)
		self.assertEqual(self._saidas()[0].conteudo, 'Domingo as 9h e 18h.')

	@override_settings(OLLAMA_API_KEY='chave-teste')
	def test_audio_sem_opcao_vai_para_a_ia_na_mesma_thread(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_ativa = True
		cfg.ia_base_conhecimento = 'Endereco: Rua X.'
		cfg.save()
		mensagem = self._audio()
		with patch.object(ia_service, 'transcrever_groq', return_value='onde fica a igreja'), \
				patch.object(atendimento_ia, 'responder') as responder, \
				patch.object(atendimento_ia, 'agendar') as agendar:
			transcricao.depois_do_download(mensagem)
		responder.assert_called_once_with(self.pessoa.pk, mensagem.pk, primeira=False)
		agendar.assert_not_called()

	@override_settings(OLLAMA_API_KEY='chave-teste')
	def test_primeiro_audio_espera_a_transcricao_e_vai_para_a_ia_como_primeira(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_ativa = True
		cfg.ia_base_conhecimento = 'Endereco: Rua X.'
		cfg.save()
		from apps.acolhimento import atendimento_bot

		PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO, bot_etapa=PrimeiroContato.BotEtapaChoices.INATIVO,
		)
		self.pessoa.refresh_from_db()
		mensagem = self._audio(aguardando=False)
		self.assertTrue(atendimento_bot.processar_entrada(self.pessoa, '(audio)', mensagem))
		self.assertEqual(self._saidas(), [])  # nada de saudacao antes de entender o audio
		mensagem.refresh_from_db()
		self.assertTrue(mensagem.metadata_resposta['bot_primeira'])
		with patch.object(ia_service, 'transcrever_groq', return_value='onde fica a igreja'), \
				patch.object(atendimento_ia, 'responder') as responder:
			transcricao.depois_do_download(mensagem)
		responder.assert_called_once_with(self.pessoa.pk, mensagem.pk, primeira=True)

	@override_settings(OLLAMA_API_KEY='chave-teste')
	def test_primeiro_audio_sem_transcricao_recebe_saudacao(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_ativa = True
		cfg.save()
		mensagem = self._audio(metadata_resposta={'bot_aguardando_transcricao': True, 'bot_primeira': True})
		with patch.object(ia_service, 'transcrever_groq', side_effect=ia_service.IAError('HTTP 400', http_status=400)), \
				patch.object(atendimento_ia, 'responder') as responder:
			transcricao.depois_do_download(mensagem)
		responder.assert_not_called()
		self.assertTrue(self._saidas()[0].conteudo.startswith(cfg.mensagem_saudacao))

	@override_settings(OLLAMA_API_KEY='chave-teste')
	def test_falha_na_transcricao_nao_manda_audio_para_a_ia(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_ativa = True
		cfg.save()
		mensagem = self._audio()
		with patch.object(ia_service, 'transcrever_groq', side_effect=ia_service.IAError('HTTP 400', http_status=400)), \
				patch.object(atendimento_ia, 'responder') as responder:
			transcricao.depois_do_download(mensagem)
		responder.assert_not_called()
		self.assertIn('1. Horario dos cultos', self._saidas()[0].conteudo)

	def test_falha_na_transcricao_mantem_o_menu(self):
		mensagem = self._audio()
		with patch.object(ia_service, 'transcrever_groq', side_effect=ia_service.IAError('HTTP 400', http_status=400)):
			transcricao.depois_do_download(mensagem)
		self.assertIn('1. Horario dos cultos', self._saidas()[0].conteudo)

	def test_pessoa_com_a_equipe_so_ganha_a_transcricao(self):
		PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		mensagem = self._audio(aguardando=False)
		with patch.object(ia_service, 'transcrever_groq', return_value='qual o horario'):
			transcricao.depois_do_download(mensagem)
		mensagem.refresh_from_db()
		self.assertEqual(mensagem.transcricao, 'qual o horario')
		self.assertEqual(self._saidas(), [])

	def test_audio_velho_nao_e_respondido(self):
		mensagem = self._audio()
		MensagemContato.objects.filter(pk=mensagem.pk).update(enfileirada_em=timezone.now() - timedelta(hours=3))
		mensagem.refresh_from_db()
		with patch.object(ia_service, 'transcrever_groq', return_value='qual o horario'):
			transcricao.depois_do_download(mensagem)
		self.assertEqual(self._saidas(), [])

	def test_audio_sem_status_pendente_e_ignorado(self):
		mensagem = self._audio(transcricao_status='')
		with patch.object(ia_service, 'transcrever_groq') as groq:
			transcricao.depois_do_download(mensagem)
		groq.assert_not_called()

	def test_download_dispara_a_transcricao(self):
		mensagem = self._audio()
		MensagemContato.objects.filter(pk=mensagem.pk).update(midia_status=MensagemContato.MidiaStatusChoices.PENDENTE)
		with patch('apps.acolhimento.meta_service.obter_url_midia', return_value={'url': 'https://x', 'mime_type': 'audio/ogg'}), \
				patch('apps.acolhimento.meta_service.baixar_midia', return_value=b'OggS'), \
				patch.object(transcricao, 'depois_do_download') as depois:
			self.assertTrue(midia_whatsapp.baixar_midia_da_mensagem(mensagem.pk))
		depois.assert_called_once()

	def test_cron_reprocessa_pendentes_antigas_e_fecha_as_sem_midia(self):
		antiga = self._audio()
		sem_midia = self._audio()
		MensagemContato.objects.filter(pk=antiga.pk).update(atualizado_em=timezone.now() - timedelta(minutes=10))
		MensagemContato.objects.filter(pk=sem_midia.pk).update(midia_status=MensagemContato.MidiaStatusChoices.FALHA)
		with patch.object(transcricao, 'depois_do_download') as depois:
			self.assertEqual(transcricao.transcrever_pendentes(), 1)
		self.assertEqual(depois.call_args.args[0].pk, antiga.pk)
		sem_midia.refresh_from_db()
		self.assertEqual(sem_midia.transcricao_status, Status.FALHA)


@override_settings(**{**META_OK, 'META_WA_APP_SECRET': ''}, DEBUG=True)
class WebhookAudioTests(_Base):
	def setUp(self):
		super().setUp()
		patcher = patch('apps.acolhimento.meta_service.marcar_como_lida', return_value=True)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_audio_fica_pendente_e_o_robo_espera_a_transcricao(self):
		payload = {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages', 'value': {
			'contacts': [{'profile': {'name': 'Bia'}, 'wa_id': '5521970002222'}],
			'messages': [{'from': '5521970002222', 'id': 'wamid.AUD', 'timestamp': '1700000000', 'type': 'audio',
						  'audio': {'id': 'MEDIA_A', 'mime_type': 'audio/ogg; codecs=opus', 'voice': True}}],
		}}]}]}
		with patch('apps.acolhimento.midia_whatsapp.threading.Thread'):
			resp = self.client.post(reverse('mensagens-webhook-meta'), data=json.dumps(payload), content_type='application/json')
		self.assertEqual(resp.status_code, 200)
		entrada = MensagemContato.objects.get(referencia_externa='wamid.AUD')
		self.assertEqual(entrada.transcricao_status, Status.PENDENTE)
		self.assertTrue(entrada.metadata_resposta['bot_aguardando_transcricao'])
		# Nada de "nao entendi" + menu antes da transcricao.
		self.assertEqual(self._saidas(), [])

	def test_reacao_fica_registrada_mas_nao_aciona_o_robo(self):
		PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO, bot_etapa=PrimeiroContato.BotEtapaChoices.INATIVO,
		)
		payload = {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages', 'value': {
			'messages': [{'from': '5521970002222', 'id': 'wamid.REAC', 'timestamp': '1700000000', 'type': 'reaction',
						  'reaction': {'message_id': 'wamid.OUT', 'emoji': '\u2764\ufe0f'}}],
		}}]}]}
		resp = self.client.post(reverse('mensagens-webhook-meta'), data=json.dumps(payload), content_type='application/json')
		self.assertEqual(resp.status_code, 200)
		entrada = MensagemContato.objects.get(referencia_externa='wamid.REAC')
		self.assertIn('reacao', entrada.conteudo)
		self.assertEqual(self._saidas(), [])
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.bot_etapa, PrimeiroContato.BotEtapaChoices.INATIVO)
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO)

	def test_transcricao_desligada_mantem_o_comportamento_antigo(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.transcrever_audios = False
		cfg.save()
		payload = {'object': 'whatsapp_business_account', 'entry': [{'changes': [{'field': 'messages', 'value': {
			'messages': [{'from': '5521970002222', 'id': 'wamid.AUD2', 'timestamp': '1700000000', 'type': 'audio',
						  'audio': {'id': 'MEDIA_B', 'mime_type': 'audio/ogg'}}],
		}}]}]}
		with patch('apps.acolhimento.midia_whatsapp.threading.Thread'):
			self.client.post(reverse('mensagens-webhook-meta'), data=json.dumps(payload), content_type='application/json')
		entrada = MensagemContato.objects.get(referencia_externa='wamid.AUD2')
		self.assertEqual(entrada.transcricao_status, '')
		self.assertIn('1. Horario dos cultos', self._saidas()[0].conteudo)


class GroqClienteTests(TestCase):
	@override_settings(**GROQ_OK)
	def test_multipart_com_modelo_idioma_e_arquivo(self):
		with patch('apps.acolhimento.ia_service.urllib.request.urlopen', return_value=_Resposta({'text': ' Oi, tudo bem? '})) as urlopen:
			texto = ia_service.transcrever_groq(b'OggS-audio', nome='midia.ogg', mime='audio/ogg; codecs=opus')
		self.assertEqual(texto, 'Oi, tudo bem?')
		req = urlopen.call_args.args[0]
		self.assertEqual(req.full_url, 'https://groq.teste/v1/audio/transcriptions')
		self.assertEqual(req.get_header('Authorization'), 'Bearer groq-teste')
		# Sem User-Agent proprio o Cloudflare da Groq responde 403 (error code 1010).
		self.assertEqual(req.get_header('User-agent'), 'acolhimento-pibvp/1.0')
		corpo = req.data
		self.assertIn(b'name="model"\r\n\r\nwhisper-large-v3-turbo', corpo)
		self.assertIn(b'name="language"\r\n\r\npt', corpo)
		self.assertIn(b'filename="midia.ogg"\r\nContent-Type: audio/ogg\r\n\r\nOggS-audio', corpo)

	@override_settings(**GROQ_OK)
	def test_429_marca_limite(self):
		with patch('apps.acolhimento.ia_service.urllib.request.urlopen',
				   side_effect=_http_error(429, {'error': {'message': 'Rate limit reached'}})):
			with self.assertRaises(ia_service.IAError) as ctx:
				ia_service.transcrever_groq(b'x', nome='a.ogg', mime='audio/ogg')
		self.assertTrue(ctx.exception.limite_ou_fora_do_ar)
		self.assertIn('Rate limit', str(ctx.exception))


class TranscricaoLocalTests(TestCase):
	def setUp(self):
		self.pasta = tempfile.mkdtemp(prefix='whisper-teste-')
		self.addCleanup(shutil.rmtree, self.pasta, ignore_errors=True)

	def _modelo(self, duracao):
		segmentos = [SimpleNamespace(text=' Oi, '), SimpleNamespace(text='tudo bem? ')]
		return SimpleNamespace(transcribe=lambda *a, **k: (iter(segmentos), SimpleNamespace(duration=duracao)))

	def test_junta_os_segmentos(self):
		with override_settings(WHISPER_MODELOS_DIR=self.pasta, TRANSCRICAO_LOCAL_ATIVA=True), \
				patch.object(transcricao_local, 'carregar_modelo', return_value=self._modelo(4.0)):
			resultado = transcricao_local.transcrever('/tmp/x.ogg')
		self.assertEqual(resultado['texto'], 'Oi, tudo bem?')
		self.assertEqual(resultado['duracao'], 4.0)

	def test_audio_longo_demais_nao_e_transcrito(self):
		with override_settings(WHISPER_MODELOS_DIR=self.pasta, TRANSCRICAO_LOCAL_ATIVA=True, TRANSCRICAO_LOCAL_MAX_SEGUNDOS=60), \
				patch.object(transcricao_local, 'carregar_modelo', return_value=self._modelo(240.0)):
			with self.assertRaises(transcricao_local.TranscricaoLocalIndisponivel):
				transcricao_local.transcrever('/tmp/x.ogg')

	def test_desligada_nao_carrega_modelo(self):
		with override_settings(TRANSCRICAO_LOCAL_ATIVA=False), \
				patch.object(transcricao_local, 'carregar_modelo') as carregar:
			with self.assertRaises(transcricao_local.TranscricaoLocalIndisponivel):
				transcricao_local.transcrever('/tmp/x.ogg')
		carregar.assert_not_called()
