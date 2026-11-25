"""Comando de cron `processar_fila`: respeita o switch "Processamento automatico"."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.acolhimento.models import ConfiguracaoProcessamentoFila, ExecucaoProcessamentoFila, MensagemContato, PrimeiroContato


class ProcessarFilaComandoTests(TestCase):
	def setUp(self):
		for alvo in ('apps.acolhimento.midia_whatsapp.baixar_midias_pendentes', 'apps.acolhimento.transcricao.transcrever_pendentes'):
			patcher = patch(alvo, return_value=0)
			self.addCleanup(patcher.stop)
			setattr(self, alvo.rsplit('.', 1)[-1], patcher.start())
		patcher = patch('apps.acolhimento.fila_auto.executar_fila_execucao')
		self.addCleanup(patcher.stop)
		self.executar = patcher.start()
		pessoa = PrimeiroContato.objects.create(
			nome='Ana', telefone_whatsapp='21900001111', primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		)
		MensagemContato.objects.create(
			pessoa=pessoa, canal='whatsapp', direcao='saida', status_fila='pendente', conteudo='Oi',
		)

	def _switch(self, ligado):
		ConfiguracaoProcessamentoFila.objects.update_or_create(pk=1, defaults={'auto_ativo': ligado})

	def _rodar(self, *args):
		saida = StringIO()
		call_command('processar_fila', *args, stdout=saida)
		return saida.getvalue()

	def test_switch_ligado_envia(self):
		self._switch(True)
		self._rodar()
		self.executar.assert_called_once()
		self.assertEqual(ExecucaoProcessamentoFila.objects.count(), 1)

	def test_switch_desligado_nao_envia_mas_reprocessa_midias(self):
		self._switch(False)
		saida = self._rodar()
		self.assertIn('Processamento automatico desligado', saida)
		self.executar.assert_not_called()
		self.assertEqual(ExecucaoProcessamentoFila.objects.count(), 0)
		self.baixar_midias_pendentes.assert_called_once()
		self.transcrever_pendentes.assert_called_once()

	def test_ignorar_switch_envia_mesmo_desligado(self):
		self._switch(False)
		self._rodar('--ignorar-switch')
		self.executar.assert_called_once()
