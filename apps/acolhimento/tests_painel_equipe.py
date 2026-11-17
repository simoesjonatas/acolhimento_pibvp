"""Painel da equipe: acolhidos, aguardando, atrasados e tempo de resposta por voluntario."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import painel_equipe
from apps.acolhimento.models import MensagemContato, PrimeiroContato
from apps.acolhimento.tests import SIMPLE_STATIC_STORAGES
from apps.core.models import PerfilUsuario

Status = PrimeiroContato.StatusAcolhimento


def _usuario(nome, *perms, **extra):
	user = get_user_model().objects.create_user(nome, password='x', first_name=nome.title(), **extra)
	for codename in perms:
		user.user_permissions.add(Permission.objects.get(codename=codename))
	return user


class _Base(TestCase):
	def setUp(self):
		self.agora = timezone.now()
		self.ana = _usuario('ana', 'pode_conversar_pessoas')
		self.bia = _usuario('bia', 'pode_conversar_pessoas')
		self._seq = 0

	def _pessoa(self, nome, responsavel=None, status=Status.EM_ACOMPANHAMENTO, dias=1):
		self._seq += 1
		pessoa = PrimeiroContato.objects.create(
			nome=nome, telefone_whatsapp=f'2190000{self._seq:04d}', primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM, status=status,
			responsavel_atual=responsavel,
		)
		PrimeiroContato.objects.filter(pk=pessoa.pk).update(criado_em=self.agora - timedelta(days=dias))
		return pessoa

	def _msg(self, pessoa, horas_atras, direcao, criado_por=None):
		msg = MensagemContato.objects.create(
			pessoa=pessoa, canal='whatsapp', direcao=direcao, status_fila='enviada',
			conteudo='oi', criado_por=criado_por,
		)
		MensagemContato.objects.filter(pk=msg.pk).update(enfileirada_em=self.agora - timedelta(hours=horas_atras))

	def _entrada(self, pessoa, horas_atras):
		self._msg(pessoa, horas_atras, 'entrada')

	def _resposta(self, pessoa, horas_atras, por=None):
		self._msg(pessoa, horas_atras, 'saida', criado_por=por or self.ana)

	def _linha(self, resumo, usuario):
		return next(linha for linha in resumo['linhas'] if linha['usuario'] == usuario)


class ResumoTests(_Base):
	def test_contagens_por_voluntario(self):
		self._pessoa('A1', self.ana)
		self._pessoa('A2 parada', self.ana, dias=20)
		esperando = self._pessoa('A3 esperando', self.ana)
		self._entrada(esperando, 30)
		self._pessoa('Membro da Ana', self.ana, status=Status.MEMBRO)
		self._pessoa('B1', self.bia)
		sem = self._pessoa('Sem dono')
		self._entrada(sem, 2)

		resumo = painel_equipe.resumo(30, agora=self.agora)
		ana = self._linha(resumo, self.ana)
		self.assertEqual((ana['acolhidos'], ana['atrasados'], ana['aguardando'], ana['aguardando_24h']), (3, 1, 1, 1))
		self.assertEqual(ana['espera_texto'], '30 h')
		self.assertEqual(self._linha(resumo, self.bia)['acolhidos'], 1)
		self.assertEqual(resumo['sem_responsavel']['aguardando'], 1)
		self.assertEqual(resumo['totais']['acolhidos'], 5)
		# Quem tem gente esperando ha +24h vem primeiro.
		self.assertEqual(resumo['linhas'][0]['usuario'], self.ana)

	def test_tempo_de_resposta_e_janela_de_24h(self):
		rapida = self._pessoa('Rapida', self.ana)
		self._entrada(rapida, 50)
		self._entrada(rapida, 49)  # mesma pendencia
		self._resposta(rapida, 48)  # respondeu em 2h
		lenta = self._pessoa('Lenta', self.ana)
		self._entrada(lenta, 40)
		self._resposta(lenta, 10, por=self.bia)  # 30h depois, por outra pessoa da equipe
		aberta_velha = self._pessoa('Aberta velha', self.ana)
		self._entrada(aberta_velha, 26)  # ninguem respondeu e ja passou de 24h
		aberta_nova = self._pessoa('Aberta nova', self.ana)
		self._entrada(aberta_nova, 3)  # ainda da tempo: nao entra na conta

		ana = self._linha(painel_equipe.resumo(30, agora=self.agora), self.ana)
		self.assertEqual(ana['respondidas'], 2)
		self.assertEqual(ana['pendencias'], 3)
		self.assertEqual(ana['em_24h'], 1)
		self.assertEqual(ana['pct_24h'], 33)
		self.assertEqual(ana['mediana_resposta'], timedelta(hours=16))
		self.assertEqual(ana['mediana_texto'], '16 h')

	def test_robo_e_disparo_em_lote_nao_contam_como_resposta(self):
		pessoa = self._pessoa('Robo', self.ana)
		self._entrada(pessoa, 30)
		self._msg(pessoa, 29, 'saida')  # robo: sem autor
		ana = self._linha(painel_equipe.resumo(30, agora=self.agora), self.ana)
		self.assertEqual(ana['respondidas'], 0)
		self.assertEqual(ana['aguardando'], 1)
		self.assertEqual(ana['pct_24h'], 0)

	def test_periodo_corta_o_que_e_antigo(self):
		pessoa = self._pessoa('Antiga', self.ana)
		self._entrada(pessoa, 24 * 10)
		self._resposta(pessoa, 24 * 10 - 1)
		self.assertEqual(self._linha(painel_equipe.resumo(7, agora=self.agora), self.ana)['respondidas'], 0)
		self.assertEqual(self._linha(painel_equipe.resumo(30, agora=self.agora), self.ana)['respondidas'], 1)

	def test_quem_esta_no_rodizio_aparece_mesmo_sem_acolhidos(self):
		cris = _usuario('cris', 'pode_conversar_pessoas')
		PerfilUsuario.objects.create(usuario=cris, recebe_novos_contatos=True)
		cris_linha = self._linha(painel_equipe.resumo(30, agora=self.agora), cris)
		self.assertTrue(cris_linha['no_rodizio'])
		self.assertFalse(cris_linha['tem_telefone'])
		self.assertEqual(cris_linha['acolhidos'], 0)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class TelaTests(_Base):
	def setUp(self):
		super().setUp()
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.url = reverse('painel-equipe')

	def test_admin_e_pastor_veem_voluntario_nao(self):
		pastor = _usuario('pastor', 'pode_ver_gabinete')
		admin = _usuario('adm', is_staff=True)
		for usuario, esperado in ((admin, 200), (pastor, 200), (self.ana, 403)):
			with self.subTest(usuario=usuario.username):
				self.client.force_login(usuario)
				self.assertEqual(self.client.get(self.url).status_code, esperado)

	def test_link_no_menu_so_para_quem_ve(self):
		self.client.force_login(_usuario('adm2', is_staff=True))
		self.assertContains(self.client.get(reverse('dashboard')), reverse('painel-equipe'))
		self.client.force_login(self.ana)
		self.assertNotContains(self.client.get(reverse('dashboard')), reverse('painel-equipe'))

	def test_tela_mostra_as_linhas_e_o_periodo(self):
		esperando = self._pessoa('Esperando', self.ana)
		self._entrada(esperando, 30)
		self.client.force_login(_usuario('adm3', is_staff=True))
		resp = self.client.get(self.url, {'dias': '7'})
		self.assertEqual(resp.context['painel']['dias'], 7)
		self.assertContains(resp, 'Ana')
		self.assertContains(resp, f'?responsavel={self.ana.pk}&ordem=espera')
		self.assertContains(resp, '1 há +24h')
		# Periodo invalido cai no padrao.
		self.assertEqual(self.client.get(self.url, {'dias': '5'}).context['painel']['dias'], 30)

	def test_filtro_responsavel_na_lista(self):
		self._pessoa('Da Ana', self.ana)
		self._pessoa('Da Bia', self.bia)
		self._pessoa('Sem dono')
		self.client.force_login(_usuario('adm4', is_staff=True))
		lista = reverse('pessoas-lista')
		self.assertEqual([p.nome for p in self.client.get(lista, {'responsavel': self.ana.pk}).context['pessoas']], ['Da Ana'])
		self.assertEqual([p.nome for p in self.client.get(lista, {'responsavel': 'sem'}).context['pessoas']], ['Sem dono'])
		self.assertContains(self.client.get(lista, {'responsavel': self.ana.pk}), 'Pessoas de Ana')

	def test_voluntario_nao_usa_o_filtro_para_ver_outros(self):
		self._pessoa('Da Ana', self.ana)
		self._pessoa('Da Bia', self.bia)
		self.client.force_login(self.ana)
		resp = self.client.get(reverse('pessoas-lista'), {'responsavel': self.bia.pk})
		self.assertEqual([p.nome for p in resp.context['pessoas']], ['Da Ana'])
