"""Painel: "Precisam de atencao" e "Meus acolhidos"; filtro na lista de pessoas."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import atencao
from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato
from apps.acolhimento.tests import SIMPLE_STATIC_STORAGES

Status = PrimeiroContato.StatusAcolhimento


def _pessoa(nome, status=Status.ROBO, dias=0, responsavel=None, telefone=None):
	pessoa = PrimeiroContato.objects.create(
		nome=nome,
		telefone_whatsapp=telefone or f'2199{abs(hash(nome)) % 10**7:07d}',
		primeira_vez=True,
		como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		status=status,
		responsavel_atual=responsavel,
	)
	PrimeiroContato.objects.filter(pk=pessoa.pk).update(criado_em=timezone.now() - timedelta(days=dias))
	pessoa.refresh_from_db()
	return pessoa


def _passo(pessoa, dias):
	passo = InteracaoAcolhimento.objects.create(pessoa=pessoa, tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO)
	InteracaoAcolhimento.objects.filter(pk=passo.pk).update(criado_em=timezone.now() - timedelta(days=dias))


def _mensagem(pessoa, dias, *, criado_por=None, status=MensagemContato.StatusFilaChoices.ENVIADA, direcao='saida'):
	msg = MensagemContato.objects.create(
		pessoa=pessoa, criado_por=criado_por, canal=MensagemContato.CanalChoices.WHATSAPP,
		direcao=direcao, status_fila=status, conteudo='oi',
	)
	MensagemContato.objects.filter(pk=msg.pk).update(enfileirada_em=timezone.now() - timedelta(days=dias))
	return msg


def _nomes(queryset):
	return [p.nome for p in queryset]


class UltimoContatoTests(TestCase):
	def setUp(self):
		self.equipe = get_user_model().objects.create_user('equipe_at', password='x')

	def test_cadastro_novo_nao_entra(self):
		_pessoa('Nova', dias=2)
		self.assertEqual(_nomes(atencao.precisam_de_atencao()), [])

	def test_cada_etapa_tem_seu_prazo(self):
		_pessoa('Visitante 8d', Status.ROBO, dias=8)
		_pessoa('Acompanhamento 8d', Status.EM_ACOMPANHAMENTO, dias=8)
		_pessoa('Acompanhamento 15d', Status.EM_ACOMPANHAMENTO, dias=15)
		_pessoa('Aprovado 20d', Status.APROVADO, dias=20)
		self.assertEqual(
			sorted(_nomes(atencao.precisam_de_atencao())), ['Acompanhamento 15d', 'Visitante 8d'],
		)

	def test_primeiro_contato_e_membro_ficam_de_fora(self):
		_pessoa('Esperando', Status.PRIMEIRO_CONTATO, dias=40)
		_pessoa('Membro', Status.MEMBRO, dias=40)
		self.assertEqual(_nomes(atencao.precisam_de_atencao()), [])

	def test_passo_registrado_conta_como_contato(self):
		pessoa = _pessoa('Com passo', dias=30)
		_passo(pessoa, dias=2)
		self.assertEqual(_nomes(atencao.precisam_de_atencao()), [])

	def test_mensagem_da_equipe_conta_mas_robo_e_falha_nao(self):
		pessoa = _pessoa('Mensagens', dias=30)
		_mensagem(pessoa, 1)  # robo: sem criado_por
		_mensagem(pessoa, 1, criado_por=self.equipe, status=MensagemContato.StatusFilaChoices.FALHA)
		_mensagem(pessoa, 1, criado_por=self.equipe, status=MensagemContato.StatusFilaChoices.CANCELADA)
		self.assertEqual(_nomes(atencao.precisam_de_atencao()), ['Mensagens'])
		_mensagem(pessoa, 1, criado_por=self.equipe)
		self.assertEqual(_nomes(atencao.precisam_de_atencao()), [])

	def test_ordem_e_dias(self):
		_pessoa('Dez dias', dias=10)
		_pessoa('Trinta dias', dias=30)
		lista = atencao.marcar(list(atencao.precisam_de_atencao()))
		self.assertEqual([p.nome for p in lista], ['Trinta dias', 'Dez dias'])
		self.assertEqual(lista[0].dias_sem_contato, 30)
		self.assertTrue(lista[0].atrasada)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class PainelTests(TestCase):
	def setUp(self):
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		usuarios = get_user_model().objects
		self.voluntario = usuarios.create_user('voluntario_at', password='x', first_name='Ana')
		self.outro = usuarios.create_user('outro_at', password='x', first_name='Beto')
		self.client.force_login(self.voluntario)

	def test_precisam_de_atencao_mostra_a_equipe_toda_para_admin(self):
		self.voluntario.is_staff = True
		self.voluntario.save()
		_pessoa('Parada minha', dias=9, responsavel=self.voluntario)
		_pessoa('Parada do Beto', dias=20, responsavel=self.outro)
		_pessoa('Parada sem dono', dias=12)
		_pessoa('Em dia', dias=1)
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.context['atencao_total'], 3)
		self.assertEqual([p.nome for p in resp.context['atencao_lista']], ['Parada do Beto', 'Parada sem dono', 'Parada minha'])
		self.assertContains(resp, 'sem responsável')
		self.assertContains(resp, 'há 20 dias')
		self.assertContains(resp, '?atencao=1')

	def test_meus_acolhidos_so_do_usuario_e_na_ordem_certa(self):
		_pessoa('Minha em dia', dias=1, responsavel=self.voluntario)
		_pessoa('Minha atrasada', dias=9, responsavel=self.voluntario)
		com_mensagem = _pessoa('Minha com mensagem', dias=2, responsavel=self.voluntario)
		_mensagem(com_mensagem, 0, direcao='entrada')
		_pessoa('Minha membro', Status.MEMBRO, dias=50, responsavel=self.voluntario)
		_pessoa('Do Beto', dias=9, responsavel=self.outro)
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(
			[p.nome for p in resp.context['meus_acolhidos']],
			['Minha com mensagem', 'Minha atrasada', 'Minha em dia'],
		)
		self.assertEqual(resp.context['meus_acolhidos_total'], 3)
		self.assertEqual(resp.context['meus_acolhidos_atrasados'], 1)
		self.assertContains(resp, 'mensagem nova')
		self.assertContains(resp, '1 sem contato além do prazo')

	def test_admin_sem_acolhidos_nao_ve_o_bloco(self):
		self.voluntario.is_staff = True
		self.voluntario.save()
		resp = self.client.get(reverse('dashboard'))
		self.assertNotContains(resp, 'id="meus-acolhidos"')
		self.assertContains(resp, 'Ninguém parado além do prazo')

	def test_voluntario_ve_so_os_seus_e_nao_a_lista_da_equipe(self):
		_pessoa('Parada do Beto', dias=20, responsavel=self.outro)
		resp = self.client.get(reverse('dashboard'))
		self.assertContains(resp, 'Ninguém com você como responsável ainda')
		self.assertNotContains(resp, 'id="precisam-de-atencao"')
		self.assertNotContains(resp, 'Parada do Beto')
		self.assertEqual(resp.context['total_pessoas'], 0)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class FiltroListaTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user('lista_at', password='x', is_staff=True)
		self.client.force_login(self.user)
		_pessoa('Parada 10', dias=10)
		_pessoa('Parada 30', dias=30)
		_pessoa('Em dia', dias=1)

	def test_filtra_e_ordena_por_quem_esta_parado_ha_mais_tempo(self):
		resp = self.client.get(reverse('pessoas-lista'), {'atencao': '1'})
		self.assertEqual([p.nome for p in resp.context['pessoas']], ['Parada 30', 'Parada 10'])
		self.assertContains(resp, 'Sem contato há 30 dias')

	def test_combina_com_meus(self):
		PrimeiroContato.objects.filter(nome='Parada 10').update(responsavel_atual=self.user)
		resp = self.client.get(reverse('pessoas-lista'), {'atencao': '1', 'meus': '1'})
		self.assertEqual([p.nome for p in resp.context['pessoas']], ['Parada 10'])

	def test_sem_filtro_nao_mostra_o_selo(self):
		resp = self.client.get(reverse('pessoas-lista'))
		self.assertNotContains(resp, 'Sem contato há')
