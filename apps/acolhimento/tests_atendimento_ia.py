"""IA do robo (Ollama Cloud): decide responder, escolher opcao ou transferir. Sem rede."""
import io
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import atendimento_bot, atendimento_ia, ia_service
from apps.acolhimento.models import (
	ConfiguracaoAtendimentoBot,
	MensagemContato,
	OpcaoAtendimentoBot,
	PrimeiroContato,
)
from apps.acolhimento.tests_meta import SIMPLE_STATIC_STORAGES, _http_error, _pessoa

IA_OK = dict(OLLAMA_API_KEY='chave-teste', OLLAMA_BASE_URL='https://ollama.teste', OLLAMA_MODELO='gemma4:31b')
BASE = 'Cultos: domingo 9h e 18h. Endereco: Rua das Flores, 100.'


class _Resposta:
	def __init__(self, body):
		self._corpo = json.dumps(body).encode('utf-8')

	def read(self, n=-1):
		return self._corpo

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False


def _chat(acao, resposta='', opcao=None):
	texto = json.dumps({'acao': acao, 'opcao': opcao, 'resposta': resposta}, ensure_ascii=False)
	return {'texto': texto, 'modelo': 'gemma4:31b', 'tokens_entrada': 900, 'tokens_saida': 40}


class _BaseIA(TestCase):
	def setUp(self):
		OpcaoAtendimentoBot.objects.all().delete()
		self.horario = OpcaoAtendimentoBot.objects.create(
			ordem=1, rotulo='Horario dos cultos', palavras_chave='horario', resposta='Domingo as 9h e 18h.',
		)
		self.falar = OpcaoAtendimentoBot.objects.create(
			ordem=2, rotulo='Falar com alguem', palavras_chave='atendente', resposta='Ja chamo alguem.',
			acao=OpcaoAtendimentoBot.Acao.TRANSFERIR_HUMANO,
		)
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.ia_ativa = True
		cfg.ia_base_conhecimento = BASE
		cfg.save()
		self.pessoa = _pessoa(
			nome='Ana Souza', telefone='21970001111',
			status=PrimeiroContato.StatusAcolhimento.ROBO, bot_etapa=PrimeiroContato.BotEtapaChoices.MENU,
		)

	def _entrada(self, texto='tem estacionamento?', **extra):
		return MensagemContato.objects.create(
			pessoa=self.pessoa, canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA, status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo=texto, **extra,
		)

	def _saidas(self):
		return list(MensagemContato.objects.filter(
			pessoa=self.pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA,
		).order_by('id'))


class InterpretarRespostaTests(TestCase):
	def test_json_puro_com_cerca_ou_no_meio_do_texto(self):
		esperado = {'acao': 'responder', 'opcao': None, 'resposta': 'Oi!'}
		self.assertEqual(atendimento_ia.interpretar_resposta('{"acao":"responder","resposta":"Oi!"}', 2), esperado)
		self.assertEqual(atendimento_ia.interpretar_resposta('```json\n{"acao":"responder","resposta":"Oi!"}\n```', 2), esperado)
		self.assertEqual(atendimento_ia.interpretar_resposta('Claro! {"acao":"responder","resposta":"Oi!"} :)', 2), esperado)

	def test_opcao_valida_e_fora_do_intervalo(self):
		self.assertEqual(atendimento_ia.interpretar_resposta('{"acao":"opcao","opcao":"2"}', 2)['opcao'], 2)
		self.assertIsNone(atendimento_ia.interpretar_resposta('{"acao":"opcao","opcao":3}', 2))
		self.assertIsNone(atendimento_ia.interpretar_resposta('{"acao":"opcao","opcao":"x"}', 2))

	def test_lixo_acao_desconhecida_ou_responder_vazio(self):
		for texto in ('nao sei', '{quebrado', '{"acao":"pular"}', '{"acao":"responder","resposta":""}', '[1,2]'):
			with self.subTest(texto=texto):
				self.assertIsNone(atendimento_ia.interpretar_resposta(texto, 2))

	def test_transferir_sem_texto_vale(self):
		self.assertEqual(atendimento_ia.interpretar_resposta('{"acao":"transferir"}', 2)['acao'], 'transferir')


@override_settings(**IA_OK)
class PromptTests(_BaseIA):
	def test_prompt_tem_regras_base_opcoes_e_historico_sem_telefone(self):
		self._entrada('qual o endereco?')
		mensagens = atendimento_ia.montar_prompt(
			ConfiguracaoAtendimentoBot.carregar(), atendimento_bot.opcoes_ativas(),
			atendimento_ia._historico(self.pessoa), atendimento_ia._primeiro_nome(self.pessoa),
		)
		system = mensagens[0]['content']
		self.assertIn('Rua das Flores', system)
		self.assertIn('1. Horario dos cultos (responde): Domingo as 9h e 18h.', system)
		self.assertIn('2. Falar com alguem (transfere para a equipe)', system)
		self.assertIn('Primeiro nome da pessoa: Ana', system)
		self.assertEqual(mensagens[-1], {'role': 'user', 'content': 'qual o endereco?'})
		self.assertNotIn('21970001111', json.dumps(mensagens))

	def test_nome_automatico_nao_vai_como_nome(self):
		self.pessoa.nome = 'Contato 21970001111'
		self.assertEqual(atendimento_ia._primeiro_nome(self.pessoa), '')


@override_settings(**IA_OK)
class ProcessarEntradaComIATests(_BaseIA):
	def test_texto_sem_opcao_agenda_a_ia_em_vez_do_menu(self):
		entrada = self._entrada()
		with patch.object(atendimento_ia, 'agendar') as agendar:
			self.assertTrue(atendimento_bot.processar_entrada(self.pessoa, entrada.conteudo, entrada))
		agendar.assert_called_once_with(self.pessoa.pk, entrada.pk, primeira=False)
		self.assertEqual(self._saidas(), [])

	def test_numero_e_palavra_chave_continuam_sem_ia(self):
		entrada = self._entrada('1')
		with patch.object(atendimento_ia, 'agendar') as agendar:
			atendimento_bot.processar_entrada(self.pessoa, '1', entrada)
		agendar.assert_not_called()
		self.assertEqual(self._saidas()[0].conteudo, 'Domingo as 9h e 18h.')

	def test_frase_longa_com_palavra_chave_vai_para_a_ia(self):
		entrada = self._entrada('que horario e o culto e tem estacionamento perto?')
		with patch.object(atendimento_ia, 'agendar') as agendar:
			atendimento_bot.processar_entrada(self.pessoa, entrada.conteudo, entrada)
		agendar.assert_called_once()
		self.assertEqual(self._saidas(), [])

	def test_palavra_chave_curta_continua_direto(self):
		entrada = self._entrada('qual o horario?')
		with patch.object(atendimento_ia, 'agendar') as agendar:
			atendimento_bot.processar_entrada(self.pessoa, entrada.conteudo, entrada)
		agendar.assert_not_called()
		self.assertEqual(self._saidas()[0].conteudo, 'Domingo as 9h e 18h.')

	def test_ia_desligada_ou_sem_chave_mantem_o_menu(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_ativa = False
		cfg.save()
		entrada = self._entrada()
		with patch.object(atendimento_ia, 'agendar') as agendar:
			atendimento_bot.processar_entrada(self.pessoa, entrada.conteudo, entrada)
			cfg.ia_ativa = True
			cfg.save()
			with override_settings(OLLAMA_API_KEY=''):
				atendimento_bot.processar_entrada(self.pessoa, entrada.conteudo, entrada)
		agendar.assert_not_called()
		self.assertEqual(len(self._saidas()), 2)
		self.assertIn('1. Horario dos cultos', self._saidas()[0].conteudo)

	def _primeiro_contato(self):
		PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO, bot_etapa=PrimeiroContato.BotEtapaChoices.INATIVO,
		)
		self.pessoa.refresh_from_db()

	def test_primeira_mensagem_de_verdade_vai_para_a_ia(self):
		# Resposta ao template ("como foi sua experiencia?"): nada de saudacao generica.
		self._primeiro_contato()
		entrada = self._entrada('Bom dia, o ministerio e uma bencao!')
		with patch.object(atendimento_ia, 'agendar') as agendar:
			self.assertTrue(atendimento_bot.processar_entrada(self.pessoa, entrada.conteudo, entrada))
		agendar.assert_called_once_with(self.pessoa.pk, entrada.pk, primeira=True)
		self.assertEqual(self._saidas(), [])
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.ROBO)
		self.assertEqual(self.pessoa.bot_etapa, PrimeiroContato.BotEtapaChoices.MENU)

	def test_primeira_mensagem_so_cumprimento_recebe_a_saudacao_fixa(self):
		for texto in ('oi', 'Bom dia!', 'Olá, tudo bem?', 'boa noite irmão', '(figurinha)'):
			with self.subTest(texto=texto):
				MensagemContato.objects.all().delete()
				self._primeiro_contato()
				entrada = self._entrada(texto)
				with patch.object(atendimento_ia, 'agendar') as agendar:
					atendimento_bot.processar_entrada(self.pessoa, texto, entrada)
				agendar.assert_not_called()
				self.assertIn('1. Horario dos cultos', self._saidas()[0].conteudo)

	def test_eh_saudacao_simples(self):
		self.assertTrue(atendimento_bot.eh_saudacao_simples('Oii, boa tarde!!'))
		self.assertFalse(atendimento_bot.eh_saudacao_simples('bom dia, queria saber das aulas de musica'))
		self.assertFalse(atendimento_bot.eh_saudacao_simples('Foi uma bencao'))

	def test_agendar_roda_em_thread_depois_do_commit(self):
		with patch('apps.acolhimento.atendimento_ia.threading.Thread') as thread:
			with self.captureOnCommitCallbacks(execute=False) as callbacks:
				atendimento_ia.agendar(self.pessoa.pk, 99)
				thread.assert_not_called()
			for callback in callbacks:
				callback()
		thread.assert_called_once()
		self.assertEqual(thread.call_args.kwargs['args'], (self.pessoa.pk, 99, False))


@override_settings(**IA_OK)
class ResponderTests(_BaseIA):
	def _responder(self, entrada, retorno=None, erro=None):
		kwargs = {'side_effect': erro} if erro else {'return_value': retorno}
		with patch.object(ia_service, 'chat', **kwargs) as chat:
			acao = atendimento_ia.responder(self.pessoa.pk, entrada.pk, esperar=False)
		return acao, chat

	def test_responder_enfileira_com_origem_e_tokens(self):
		acao, chat = self._responder(self._entrada(), _chat('responder', 'Temos sim, na rua lateral.'))
		self.assertEqual(acao, 'responder')
		saida = self._saidas()[0]
		self.assertEqual(saida.conteudo, 'Temos sim, na rua lateral.')
		self.assertEqual(saida.metadata_envio['origem'], 'bot_ia')
		self.assertEqual(saida.metadata_envio['ia']['tokens_entrada'], 900)
		self.assertEqual(chat.call_args.kwargs['modelo'], 'gemma4:31b')

	def test_modelo_da_tela_tem_prioridade(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_modelo = 'gpt-oss:20b'
		cfg.save()
		_acao, chat = self._responder(self._entrada(), _chat('responder', 'Oi'))
		self.assertEqual(chat.call_args.kwargs['modelo'], 'gpt-oss:20b')

	def test_opcao_envia_a_resposta_oficial(self):
		acao, _ = self._responder(self._entrada('que horas e a missa'), _chat('opcao', opcao=1))
		self.assertEqual(acao, 'opcao')
		self.assertEqual(self._saidas()[0].conteudo, 'Domingo as 9h e 18h.')
		self.assertEqual(self._saidas()[0].metadata_envio['origem'], 'bot_ia')

	def test_opcao_de_transferir_tira_do_robo(self):
		self._responder(self._entrada('quero falar com o pastor'), _chat('opcao', opcao=2))
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)

	def test_transferir_manda_aviso_e_passa_para_a_equipe(self):
		acao, _ = self._responder(self._entrada('minha mae faleceu'), _chat('transferir', 'Sinto muito. Vou chamar alguem.'))
		self.assertEqual(acao, 'transferir')
		self.assertEqual(self._saidas()[0].conteudo, 'Sinto muito. Vou chamar alguem.')
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		self.assertEqual(self.pessoa.bot_etapa, PrimeiroContato.BotEtapaChoices.INATIVO)

	def test_erro_ou_resposta_ilegivel_caem_no_menu(self):
		for erro, retorno in (
			(ia_service.IAError('HTTP 429', http_status=429), None),
			(ia_service.IAError('timeout'), None),
			(None, {'texto': 'desculpe, nao entendi', 'modelo': 'x', 'tokens_entrada': 1, 'tokens_saida': 1}),
		):
			with self.subTest(erro=erro, retorno=retorno):
				MensagemContato.objects.filter(direcao=MensagemContato.DirecaoChoices.SAIDA).delete()
				acao, _ = self._responder(self._entrada(), retorno, erro)
				self.assertEqual(acao, 'fallback')
				saida = self._saidas()[0]
				self.assertIn('1. Horario dos cultos', saida.conteudo)
				self.assertEqual(saida.metadata_envio['origem'], 'bot_atendimento')

	def test_primeira_resposta_vai_com_o_menu_e_avisa_a_ia(self):
		with patch.object(ia_service, 'chat', return_value=_chat('responder', 'Que bom que voce gostou, Ana!')) as chat:
			acao = atendimento_ia.responder(self.pessoa.pk, self._entrada('Foi uma bencao!').pk, esperar=False, primeira=True)
		self.assertEqual(acao, 'responder')
		self.assertIn('PRIMEIRA RESPOSTA', chat.call_args.args[0][0]['content'])
		conteudo = self._saidas()[0].conteudo
		self.assertTrue(conteudo.startswith('Que bom que voce gostou, Ana!'))
		self.assertIn('1. Horario dos cultos', conteudo)

	def test_primeira_resposta_com_erro_manda_saudacao_e_menu(self):
		acao, _ = self._responder(self._entrada('Foi uma bencao!'), erro=ia_service.IAError('HTTP 503', http_status=503))
		self.assertEqual(acao, 'fallback')  # sem primeira: nao entendi + menu
		MensagemContato.objects.filter(direcao=MensagemContato.DirecaoChoices.SAIDA).delete()
		with patch.object(ia_service, 'chat', side_effect=ia_service.IAError('HTTP 503', http_status=503)):
			atendimento_ia.responder(self.pessoa.pk, self._entrada('Foi uma bencao!').pk, esperar=False, primeira=True)
		conteudo = self._saidas()[0].conteudo
		self.assertTrue(conteudo.startswith(ConfiguracaoAtendimentoBot.carregar().mensagem_saudacao))
		self.assertIn('1. Horario dos cultos', conteudo)

	def test_erro_da_ia_ainda_tenta_a_palavra_chave(self):
		acao, _ = self._responder(
			self._entrada('que horario e o culto e tem estacionamento?'), erro=ia_service.IAError('HTTP 503', http_status=503),
		)
		self.assertEqual(acao, 'fallback_opcao')
		self.assertEqual(self._saidas()[0].conteudo, 'Domingo as 9h e 18h.')

	def test_debounce_so_responde_a_mensagem_mais_nova(self):
		primeira = self._entrada('oi')
		self._entrada('tem estacionamento?')
		acao, chat = self._responder(primeira, _chat('responder', 'x'))
		self.assertEqual(acao, 'debounce')
		chat.assert_not_called()
		self.assertEqual(self._saidas(), [])

	def test_limite_do_dia_transfere(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_max_respostas_por_dia = 2
		cfg.save()
		for _ in range(2):
			atendimento_bot.enfileirar_resposta(self.pessoa, 'resp', origem='bot_ia')
		acao, chat = self._responder(self._entrada(), _chat('responder', 'x'))
		self.assertEqual(acao, 'limite')
		chat.assert_not_called()
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)

	def test_pessoa_com_a_equipe_nao_e_respondida(self):
		entrada = self._entrada()
		PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		acao, chat = self._responder(entrada, _chat('responder', 'x'))
		self.assertEqual(acao, 'ignorado')
		chat.assert_not_called()

	def test_audio_entra_no_historico_pela_transcricao(self):
		entrada = self._entrada('(audio)', midia_tipo='audio', transcricao='que horas e o culto')
		_acao, chat = self._responder(entrada, _chat('opcao', opcao=1))
		mensagens = chat.call_args.args[0]
		self.assertEqual(mensagens[-1]['content'], '(audio) que horas e o culto')


@override_settings(**IA_OK)
class OllamaClienteTests(TestCase):
	def test_payload_e_leitura_da_resposta(self):
		corpo = {'model': 'gemma4:31b', 'message': {'role': 'assistant', 'content': ' {"acao":"responder"} '},
				 'prompt_eval_count': 120, 'eval_count': 12}
		with patch('apps.acolhimento.ia_service.urllib.request.urlopen', return_value=_Resposta(corpo)) as urlopen:
			resultado = ia_service.chat([{'role': 'user', 'content': 'oi'}], modelo='gemma4:31b')
		req = urlopen.call_args.args[0]
		self.assertEqual(req.full_url, 'https://ollama.teste/api/chat')
		self.assertEqual(req.get_header('Authorization'), 'Bearer chave-teste')
		payload = json.loads(req.data)
		self.assertEqual(payload['stream'], False)
		self.assertEqual(payload['think'], False)
		self.assertEqual(resultado, {'texto': '{"acao":"responder"}', 'modelo': 'gemma4:31b', 'tokens_entrada': 120, 'tokens_saida': 12})

	def test_429_vira_erro_de_limite(self):
		with patch('apps.acolhimento.ia_service.urllib.request.urlopen',
				   side_effect=_http_error(429, {'error': 'you have reached your usage limit'})):
			with self.assertRaises(ia_service.IAError) as ctx:
				ia_service.chat([{'role': 'user', 'content': 'oi'}])
		self.assertEqual(ctx.exception.http_status, 429)
		self.assertTrue(ctx.exception.limite_ou_fora_do_ar)
		self.assertIn('usage limit', str(ctx.exception))

	@override_settings(OLLAMA_API_KEY='')
	def test_sem_chave_nao_chama_a_rede(self):
		with patch('apps.acolhimento.ia_service.urllib.request.urlopen') as urlopen:
			with self.assertRaises(ia_service.IAError):
				ia_service.chat([{'role': 'user', 'content': 'oi'}])
		urlopen.assert_not_called()


@override_settings(**IA_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class TelaIATests(_BaseIA):
	def setUp(self):
		super().setUp()
		self.admin = get_user_model().objects.create_superuser('admin_ia', 'admin_ia@ex.com', 'x')
		self.client.force_login(self.admin)
		self.url = reverse('configuracao-atendimento')

	def _dados(self, **extra):
		dados = {
			'acao': 'ia', 'ia_ativa': 'on', 'transcrever_audios': 'on', 'ia_modelo': '',
			'ia_max_respostas_por_dia': '6', 'ia_base_conhecimento': BASE, 'ia_instrucoes': '',
		}
		dados.update(extra)
		return {chave: valor for chave, valor in dados.items() if valor is not None}

	def test_mostra_o_cartao_e_o_estado_das_chaves(self):
		resp = self.client.get(self.url)
		self.assertContains(resp, 'Inteligencia artificial')
		self.assertContains(resp, 'Ollama (texto): chave configurada')
		self.assertContains(resp, 'falta GROQ_API_KEY')

	def test_salva_a_configuracao(self):
		resp = self.client.post(self.url, self._dados())
		self.assertEqual(resp.status_code, 302)
		cfg = ConfiguracaoAtendimentoBot.carregar()
		self.assertTrue(cfg.transcrever_audios)
		self.assertEqual(cfg.ia_max_respostas_por_dia, 6)

	def test_ligar_sem_base_de_conhecimento_nao_salva(self):
		resp = self.client.post(self.url, self._dados(ia_base_conhecimento=''))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Preencha a base de conhecimento')

	def test_testar_mostra_a_decisao_sem_enviar_nada(self):
		with patch.object(ia_service, 'chat', return_value=_chat('opcao', opcao=1)):
			resp = self.client.post(self.url, {'acao': 'testar_ia', 'pergunta': 'que horas e o culto?'})
		self.assertContains(resp, 'Escolheu a opcao "Horario dos cultos"')
		self.assertContains(resp, 'Domingo as 9h e 18h.')
		self.assertFalse(MensagemContato.objects.exists())

	def test_testar_mostra_erro_da_ia(self):
		with patch.object(ia_service, 'chat', side_effect=ia_service.IAError('HTTP 401: unauthorized', http_status=401)):
			resp = self.client.post(self.url, {'acao': 'testar_ia', 'pergunta': 'oi'})
		self.assertContains(resp, 'HTTP 401: unauthorized')

	def test_so_superusuario(self):
		equipe = get_user_model().objects.create_user('equipe_ia', password='x', is_staff=True)
		self.client.force_login(equipe)
		self.assertEqual(self.client.post(self.url, self._dados()).status_code, 403)
		self.assertEqual(self.client.post(self.url, {'acao': 'testar_ia', 'pergunta': 'oi'}).status_code, 403)
