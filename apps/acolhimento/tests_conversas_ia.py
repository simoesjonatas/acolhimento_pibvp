"""Historico da IA na mensagem da pessoa e a tela Conversas da IA."""
import importlib
from datetime import timedelta
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import atendimento_bot, atendimento_ia, conversas_ia, ia_service
from apps.acolhimento.models import ConfiguracaoAtendimentoBot, MensagemContato
from apps.acolhimento.tests_atendimento_ia import IA_OK, _BaseIA, _chat
from apps.acolhimento.tests_meta import SIMPLE_STATIC_STORAGES


@override_settings(**IA_OK)
class RegistroTests(_BaseIA):
	def _responder(self, entrada, retorno=None, erro=None):
		kwargs = {'side_effect': erro} if erro else {'return_value': retorno}
		with patch.object(ia_service, 'chat', **kwargs):
			atendimento_ia.responder(self.pessoa.pk, entrada.pk, esperar=False)
		entrada.refresh_from_db()
		return entrada.metadata_resposta['ia']

	def test_responder_guarda_decisao_resposta_e_tokens(self):
		ia = self._responder(self._entrada(), _chat('responder', 'Temos sim.'))
		saida = MensagemContato.objects.get(pk=ia['resposta_id'])
		self.assertEqual(saida.conteudo, 'Temos sim.')
		self.assertEqual((ia['acao'], ia['modelo'], ia['tokens_entrada'], ia['tokens_saida']), ('responder', 'gemma4:31b', 900, 40))

	def test_opcao_guarda_o_rotulo_e_a_mensagem(self):
		ia = self._responder(self._entrada('que horas e o culto?'), _chat('opcao', opcao=1))
		self.assertEqual((ia['acao'], ia['opcao']), ('opcao', 'Horario dos cultos'))
		saida = MensagemContato.objects.get(pk=ia['resposta_id'])
		self.assertEqual(saida.conteudo, 'Domingo as 9h e 18h.')
		self.assertEqual(saida.metadata_envio['ia']['tokens_entrada'], 900)

	def test_transferir(self):
		ia = self._responder(self._entrada('minha mae faleceu'), _chat('transferir', 'Vou chamar alguem.'))
		self.assertEqual(ia['acao'], 'transferir')
		self.assertEqual(MensagemContato.objects.get(pk=ia['resposta_id']).conteudo, 'Vou chamar alguem.')

	def test_falha_e_ilegivel_guardam_o_erro_e_o_caminho(self):
		ia = self._responder(self._entrada('blabla'), erro=ia_service.IAError('HTTP 429: limite', http_status=429))
		self.assertEqual((ia['acao'], ia['caminho']), ('falha', 'fallback'))
		self.assertIn('429', ia['erro'])
		ia = self._responder(self._entrada('outra'), {'texto': 'nao sei', 'modelo': 'm', 'tokens_entrada': 1, 'tokens_saida': 1})
		self.assertEqual((ia['acao'], ia['bruto']), ('ilegivel', 'nao sei'))

	def test_limite(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_max_respostas_por_dia = 1
		cfg.save()
		atendimento_bot.enfileirar_resposta(self.pessoa, 'resp', origem='bot_ia')
		ia = self._responder(self._entrada(), _chat('responder', 'x'))
		self.assertEqual(ia['acao'], 'limite')

	def test_nao_apaga_o_que_a_transcricao_gravou(self):
		entrada = self._entrada('', midia_tipo=MensagemContato.MidiaTipoChoices.AUDIO, transcricao='tem culto hoje?')
		MensagemContato.objects.filter(pk=entrada.pk).update(metadata_resposta={'transcricao': {'motor': 'groq'}})
		self._responder(entrada, _chat('responder', 'Hoje nao.'))
		self.assertEqual(entrada.metadata_resposta['transcricao'], {'motor': 'groq'})


class MigracaoTests(_BaseIA):
	def test_copia_o_historico_das_saidas_antigas(self):
		migracao = importlib.import_module('apps.acolhimento.migrations.0036_historico_ia')
		com_id = self._entrada('tem estacionamento?')
		MensagemContato.objects.create(
			pessoa=self.pessoa, canal='whatsapp', direcao='saida', status_fila='enviada', conteudo='Temos sim.',
			metadata_envio={'origem': 'bot_ia', 'ia': {
				'acao': 'responder', 'entrada_id': com_id.pk, 'modelo': 'gemma4:31b', 'tokens_entrada': 10, 'tokens_saida': 2,
			}},
		)
		sem_id = self._entrada('que horas e o culto')
		opcao = MensagemContato.objects.create(
			pessoa=self.pessoa, canal='whatsapp', direcao='saida', status_fila='enviada', conteudo='Domingo 9h.',
			metadata_envio={'origem': 'bot_ia'},
		)
		migracao.preencher(django_apps, None)
		com_id.refresh_from_db()
		sem_id.refresh_from_db()
		self.assertEqual(com_id.metadata_resposta['ia']['acao'], 'responder')
		self.assertEqual(com_id.metadata_resposta['ia']['tokens_entrada'], 10)
		self.assertEqual(sem_id.metadata_resposta['ia'], {
			'acao': 'opcao', 'resposta_id': opcao.pk, 'em': sem_id.metadata_resposta['ia']['em'],
		})
		# Rodar de novo nao duplica nem sobrescreve.
		migracao.preencher(django_apps, None)
		sem_id.refresh_from_db()
		self.assertEqual(sem_id.metadata_resposta['ia']['resposta_id'], opcao.pk)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class TelaTests(_BaseIA):
	def setUp(self):
		super().setUp()
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.admin = get_user_model().objects.create_user('adm_ia', password='x', is_staff=True)
		self.client.force_login(self.admin)
		self.url = reverse('conversas-ia')
		self._atendido('Tem estacionamento?', 'responder', resposta='Temos sim.', tokens=(900, 40))
		self._atendido('Minha mae faleceu', 'transferir', resposta='Vou chamar alguem.')
		self._atendido('blabla', 'falha', erro='HTTP 429')
		self._atendido('antiga', 'responder', horas=24 * 10)
		self._entrada('sem IA nenhuma')

	def _atendido(self, texto, acao, resposta=None, horas=1, tokens=(0, 0), **extra):
		entrada = self._entrada(texto)
		ia = {'acao': acao, 'tokens_entrada': tokens[0], 'tokens_saida': tokens[1], **extra}
		if resposta:
			ia['resposta_id'] = atendimento_bot.enfileirar_resposta(self.pessoa, resposta, origem='bot_ia').pk
		MensagemContato.objects.filter(pk=entrada.pk).update(
			metadata_resposta={'ia': ia}, enfileirada_em=timezone.now() - timedelta(hours=horas),
		)

	def test_lista_os_atendimentos_do_periodo_com_resumo(self):
		resp = self.client.get(self.url)
		# Mais recentes primeiro.
		self.assertEqual([a.pergunta for a in resp.context['atendimentos']], ['blabla', 'Minha mae faleceu', 'Tem estacionamento?'])
		resumo = resp.context['resumo']
		self.assertEqual((resumo['total'], resumo['resolvidas'], resumo['equipe'], resumo['falhas']), (3, 1, 1, 1))
		self.assertEqual(resumo['pct_resolvidas'], 33)
		self.assertContains(resp, 'Temos sim.')
		self.assertContains(resp, 'Passou para a equipe')
		self.assertContains(resp, 'Erro: HTTP 429')
		self.assertEqual(self.client.get(self.url, {'dias': 'todos'}).context['resumo']['total'], 4)

	def test_filtros_e_busca(self):
		resp = self.client.get(self.url, {'filtro': 'falhas'})
		self.assertEqual([a.pergunta for a in resp.context['atendimentos']], ['blabla'])
		resp = self.client.get(self.url, {'q': 'estaciona'})
		self.assertEqual([a.pergunta for a in resp.context['atendimentos']], ['Tem estacionamento?'])

	def test_so_admin(self):
		voluntario = get_user_model().objects.create_user('vol_ia', password='x')
		self.client.force_login(voluntario)
		self.assertEqual(self.client.get(self.url).status_code, 403)

	def test_resumo_direto(self):
		self.assertEqual(conversas_ia.resumo(conversas_ia.atendimentos(dias=None))['tokens_entrada'], 900)
