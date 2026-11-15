"""Status "Em gabinete": entre Ingressante e Aprovado."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import atendimento_bot
from apps.acolhimento.models import ConfiguracaoAtendimentoBot, MensagemContato, OpcaoAtendimentoBot, PrimeiroContato
from apps.acolhimento.tests_meta import SIMPLE_STATIC_STORAGES, _pessoa

Status = PrimeiroContato.StatusAcolhimento


class OrdemDoStatusTests(TestCase):
	def test_fica_entre_ingressante_e_aprovado(self):
		valores = list(Status.values)
		self.assertEqual(valores.index('em_gabinete'), valores.index('participante') + 1)
		self.assertEqual(valores.index('aprovado'), valores.index('em_gabinete') + 1)
		self.assertEqual(Status.EM_GABINETE.label, 'Em gabinete')

	def test_robo_nao_intervem_em_quem_esta_em_gabinete(self):
		OpcaoAtendimentoBot.objects.create(ordem=1, rotulo='Horario', resposta='9h')
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.save()
		pessoa = _pessoa(status=Status.EM_GABINETE)
		self.assertFalse(atendimento_bot.processar_entrada(pessoa, 'oi'))
		self.assertFalse(MensagemContato.objects.filter(pessoa=pessoa).exists())


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class TelasStatusEmGabineteTests(TestCase):
	def setUp(self):
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.user = get_user_model().objects.create_superuser('admin_gab', 'admin_gab@ex.com', 'x')
		self.client.force_login(self.user)
		self.pessoa = _pessoa(status=Status.PARTICIPANTE)

	def test_muda_para_em_gabinete_e_registra_no_historico(self):
		resp = self.client.post(reverse('pessoas-status', args=[self.pessoa.pk]), {'status': 'em_gabinete'})
		self.assertEqual(resp.status_code, 302)
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, Status.EM_GABINETE)
		self.assertTrue(self.pessoa.interacoes.filter(descricao__contains='"Ingressante" para "Em gabinete"').exists())

	def test_ficha_e_lista_mostram_o_status(self):
		PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(status=Status.EM_GABINETE)
		resp = self.client.get(reverse('pessoas-detalhe', args=[self.pessoa.pk]))
		self.assertContains(resp, 'person-status-em_gabinete')
		resp = self.client.get(reverse('pessoas-lista'), {'status': 'em_gabinete'})
		self.assertContains(resp, self.pessoa.nome)

	def test_painel_tem_o_card(self):
		PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(status=Status.EM_GABINETE)
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.context['total_em_gabinete'], 1)
		self.assertContains(resp, '?status=em_gabinete')
