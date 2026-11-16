"""Tela de conversa ao vivo: consulta periodica de mensagens novas e alteradas."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento.models import MensagemContato, PrimeiroContato
from apps.acolhimento.tests_meta import _pessoa


def _mensagem(pessoa, direcao='entrada', **extra):
	dados = dict(
		pessoa=pessoa,
		canal=MensagemContato.CanalChoices.WHATSAPP,
		direcao=direcao,
		status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
		conteudo='oi',
	)
	dados.update(extra)
	return MensagemContato.objects.create(**dados)


class MensagensNovasViewTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user('equipe', password='x')
		self.user.user_permissions.add(Permission.objects.get(codename='pode_conversar_pessoas'))
		self.client.force_login(self.user)
		self.pessoa = _pessoa()
		PrimeiroContato.objects.filter(pk=self.pessoa.pk).update(responsavel_atual=self.user)
		self.url = reverse('pessoas-mensagens-novas', args=[self.pessoa.pk])

	def test_sem_permissao_bloqueado(self):
		outro = get_user_model().objects.create_user('sem', password='x')
		self.client.force_login(outro)
		self.assertEqual(self.client.get(self.url).status_code, 403)

	def test_pagina_tem_url_de_consulta_e_versao(self):
		msg = _mensagem(self.pessoa)
		resp = self.client.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		self.assertContains(resp, self.url)
		self.assertContains(resp, f'data-versao="{msg.versao_conversa}"')

	def test_devolve_so_as_mensagens_depois_do_ultimo_id(self):
		antiga = _mensagem(self.pessoa, conteudo='antiga')
		nova = _mensagem(self.pessoa, conteudo='chegou agora')
		_mensagem(_pessoa(nome='Outra', telefone='21971340000'), conteudo='de outra pessoa')

		data = self.client.get(self.url, {'depois': antiga.pk}).json()

		self.assertIn('chegou agora', data['novas'])
		self.assertNotIn('antiga', data['novas'])
		self.assertNotIn('de outra pessoa', data['novas'])
		self.assertEqual(data['ultimo_id'], nova.pk)
		self.assertEqual(data['novas_entrada'], 1)

	def test_sem_novidade_volta_vazio(self):
		msg = _mensagem(self.pessoa)
		data = self.client.get(self.url, {'depois': msg.pk, 'v': f'{msg.pk}:{msg.versao_conversa}'}).json()
		self.assertEqual(data['novas'], '')
		self.assertEqual(data['atualizadas'], {})
		self.assertEqual(data['removidas'], [])
		self.assertEqual(data['ultimo_id'], msg.pk)

	def test_status_alterado_volta_redesenhado(self):
		saida = _mensagem(self.pessoa, direcao='saida', status_fila=MensagemContato.StatusFilaChoices.PENDENTE)
		versao_antiga = saida.versao_conversa
		MensagemContato.objects.filter(pk=saida.pk).update(
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			entregue_em=timezone.now(),
		)

		data = self.client.get(self.url, {'depois': saida.pk, 'v': f'{saida.pk}:{versao_antiga}'}).json()

		self.assertIn(str(saida.pk), data['atualizadas'])
		self.assertIn('Entregue', data['atualizadas'][str(saida.pk)])

	def test_mensagem_apagada_vem_em_removidas(self):
		msg = _mensagem(self.pessoa)
		msg_id, versao = msg.pk, msg.versao_conversa
		msg.delete()
		data = self.client.get(self.url, {'v': f'{msg_id}:{versao}'}).json()
		self.assertEqual(data['removidas'], [msg_id])

	def test_nao_vaza_mensagem_de_outra_pessoa_pelas_versoes(self):
		alheia = _mensagem(_pessoa(nome='Outra', telefone='21971340000'), conteudo='segredo')
		data = self.client.get(self.url, {'v': f'{alheia.pk}:xxxx'}).json()
		self.assertEqual(data['atualizadas'], {})
		self.assertEqual(data['removidas'], [alheia.pk])

	def test_parametros_invalidos_nao_quebram(self):
		_mensagem(self.pessoa, conteudo='primeira')
		data = self.client.get(self.url, {'depois': 'abc', 'v': 'x:y,,:,12', 'canal': '<script>'}).json()
		self.assertIn('primeira', data['novas'])

	def test_visivel_marca_entrada_como_vista(self):
		msg = _mensagem(self.pessoa)
		self.client.get(self.url, {'depois': msg.pk})
		msg.refresh_from_db()
		self.assertIsNone(msg.visualizada_equipe_em)

		self.client.get(self.url, {'depois': msg.pk, 'visivel': '1'})
		msg.refresh_from_db()
		self.assertIsNotNone(msg.visualizada_equipe_em)

	def test_informa_janela(self):
		data = self.client.get(self.url).json()
		self.assertFalse(data['janela_aberta'])
		_mensagem(self.pessoa)
		data = self.client.get(self.url).json()
		self.assertTrue(data['janela_aberta'])
