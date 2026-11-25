"""Aviso de numero que o WhatsApp nao entrega (#131026 da Meta)."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import numero_whatsapp
from apps.acolhimento.models import MensagemContato, PrimeiroContato
from apps.acolhimento.tests import SIMPLE_STATIC_STORAGES, _dados_form

ERRO_131026 = 'Meta #131026: Message undeliverable - Message Undeliverable.'


def _pessoa(nome='Marcia', telefone='21995402373'):
	return PrimeiroContato.objects.create(
		nome=nome, telefone_whatsapp=telefone, primeira_vez=True,
		como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		status=PrimeiroContato.StatusAcolhimento.ROBO,
	)


def _falha(pessoa, erro=ERRO_131026, horas=2, tipo='primeiro_contato_opt_in'):
	msg = MensagemContato.objects.create(
		pessoa=pessoa, canal='whatsapp', direcao='saida', status_fila='falha',
		conteudo='Ola!', erro_ultimo_envio=erro, metadata_envio={'tipo_template': tipo},
	)
	MensagemContato.objects.filter(pk=msg.pk).update(atualizado_em=timezone.now() - timedelta(hours=horas))
	return msg


class RegraTests(TestCase):
	def test_falha_de_numero_gera_aviso(self):
		pessoa = _pessoa()
		_falha(pessoa)
		falha = numero_whatsapp.falha_do_numero(pessoa)
		self.assertIsNotNone(falha)
		self.assertEqual(falha.rotulo_tipo, 'a mensagem de boas-vindas')

	def test_outros_erros_nao_contam(self):
		pessoa = _pessoa()
		_falha(pessoa, erro='Fora da janela de 24h (Meta #131047: Re-engagement message)')
		self.assertIsNone(numero_whatsapp.falha_do_numero(pessoa))

	def test_some_quando_ela_escreve_depois(self):
		pessoa = _pessoa()
		_falha(pessoa)
		MensagemContato.objects.create(pessoa=pessoa, canal='whatsapp', direcao='entrada', status_fila='enviada', conteudo='oi')
		self.assertIsNone(numero_whatsapp.falha_do_numero(pessoa))

	def test_some_quando_outra_mensagem_e_entregue_depois(self):
		pessoa = _pessoa()
		_falha(pessoa)
		MensagemContato.objects.create(
			pessoa=pessoa, canal='whatsapp', direcao='saida', status_fila='enviada', conteudo='oi',
			entregue_em=timezone.now(),
		)
		self.assertIsNone(numero_whatsapp.falha_do_numero(pessoa))

	def test_falhas_por_pessoa_em_lote(self):
		com, sem = _pessoa('Com', '21900000001'), _pessoa('Sem', '21900000002')
		_falha(com)
		self.assertEqual(set(numero_whatsapp.falhas_por_pessoa([com.pk, sem.pk])), {com.pk})


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class TelasTests(TestCase):
	def setUp(self):
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.admin = get_user_model().objects.create_user('adm_num', password='x', is_staff=True)
		self.client.force_login(self.admin)
		self.pessoa = _pessoa()
		_falha(self.pessoa)

	def test_ficha_conversa_e_lista_avisam(self):
		ficha = self.client.get(reverse('pessoas-detalhe', args=[self.pessoa.pk]))
		self.assertContains(ficha, 'O WhatsApp não entregou a mensagem de boas-vindas')
		self.assertContains(ficha, 'Corrigir telefone')
		conversa = self.client.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		self.assertContains(conversa, 'O número pode não ter WhatsApp')
		lista = self.client.get(reverse('pessoas-lista'))
		self.assertContains(lista, 'WhatsApp não recebe')

	def test_corrigir_o_telefone_tira_o_aviso(self):
		dados = _dados_form('21995402374', nome='Marcia', sobrenome='')
		dados.update({'nome': 'Marcia', 'o_que_busca': PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS})
		dados.pop('sobrenome')
		resp = self.client.post(reverse('pessoas-editar', args=[self.pessoa.pk]), dados)
		self.assertEqual(resp.status_code, 302, getattr(resp, 'context', None) and resp.context['form'].errors)
		self.assertNotContains(self.client.get(reverse('pessoas-detalhe', args=[self.pessoa.pk])), 'O WhatsApp não entregou')

	def test_salvar_sem_mudar_o_telefone_mantem_o_aviso(self):
		dados = _dados_form('21995402373', nome='Marcia', sobrenome='')
		dados.update({'nome': 'Marcia Silva', 'o_que_busca': PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS})
		dados.pop('sobrenome')
		self.client.post(reverse('pessoas-editar', args=[self.pessoa.pk]), dados)
		self.assertContains(self.client.get(reverse('pessoas-detalhe', args=[self.pessoa.pk])), 'O WhatsApp não entregou')
