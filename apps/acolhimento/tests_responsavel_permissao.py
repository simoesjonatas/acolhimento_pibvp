"""Quem nao atende mensagens nao pode ficar como responsavel (a pessoa ficava presa)."""
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import rodizio
from apps.acolhimento.models import InteracaoAcolhimento, PrimeiroContato
from apps.acolhimento.tests import SIMPLE_STATIC_STORAGES, _dados_form
from apps.core.models import PerfilUsuario

Status = PrimeiroContato.StatusAcolhimento


def _usuario(nome, *perms, rodizio_ligado=False, **extra):
	user = get_user_model().objects.create_user(nome, password='x', first_name=nome.title(), **extra)
	for codename in perms:
		user.user_permissions.add(Permission.objects.get(codename=codename))
	if rodizio_ligado:
		PerfilUsuario.objects.create(usuario=user, telefone='(21) 98888-7777', recebe_novos_contatos=True)
	return get_user_model().objects.get(pk=user.pk)


class AtendeMensagensTests(TestCase):
	def test_regra(self):
		self.assertTrue(rodizio.atende_mensagens(_usuario('vol', 'pode_conversar_pessoas')))
		self.assertTrue(rodizio.atende_mensagens(_usuario('pastor', 'pode_ver_gabinete')))
		self.assertTrue(rodizio.atende_mensagens(_usuario('adm', is_staff=True)))
		self.assertFalse(rodizio.atende_mensagens(_usuario('so_cadastra')))
		self.assertFalse(rodizio.atende_mensagens(_usuario('inativo', 'pode_conversar_pessoas', is_active=False)))


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class CadastroTests(TestCase):
	def setUp(self):
		patcher = patch.object(rodizio, '_avisar_em_thread')
		patcher.start()
		self.addCleanup(patcher.stop)
		self.recepcao = _usuario('recepcao')

	def _cadastrar(self, usuario):
		self.client.force_login(usuario)
		resp = self.client.post(reverse('pessoas-novo'), _dados_form('21977776666', nome='Joana'), follow=True)
		return PrimeiroContato.objects.get(nome='Joana Silva'), resp

	def test_quem_nao_atende_cadastra_e_a_pessoa_fica_sem_responsavel(self):
		# Nem com gente no rodizio: ela so entra no rodizio quando interagir e pedir alguem.
		_usuario('ana', 'pode_conversar_pessoas', rodizio_ligado=True)
		pessoa, resp = self._cadastrar(self.recepcao)
		self.assertIsNone(pessoa.responsavel_atual)
		self.assertEqual(pessoa.criado_por, self.recepcao)
		self.assertContains(resp, 'fica sem responsável até falar com o atendimento')

	def test_quem_atende_continua_responsavel(self):
		_usuario('ana', 'pode_conversar_pessoas', rodizio_ligado=True)
		bia = _usuario('bia', 'pode_conversar_pessoas')
		pessoa, _ = self._cadastrar(bia)
		self.assertEqual(pessoa.responsavel_atual, bia)


class ComandoCorrigirTests(TestCase):
	def setUp(self):
		self.recepcao = _usuario('recepcao')
		self.vol = _usuario('vol', 'pode_conversar_pessoas')
		self.ana = _usuario('ana', 'pode_conversar_pessoas', rodizio_ligado=True)
		self.presa = self._pessoa('Presa', self.recepcao)
		self.membro = self._pessoa('Membro', self.recepcao, status=Status.MEMBRO)
		self.certa = self._pessoa('Certa', self.vol)

	def _pessoa(self, nome, responsavel, status=Status.EM_ACOMPANHAMENTO):
		return PrimeiroContato.objects.create(
			nome=nome, telefone_whatsapp=f'2190000{PrimeiroContato.objects.count():04d}', primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM, status=status, responsavel_atual=responsavel,
		)

	def _rodar(self, *args):
		saida = StringIO()
		call_command('corrigir_responsaveis', *args, stdout=saida)
		return saida.getvalue()

	def test_sem_aplicar_so_mostra(self):
		saida = self._rodar()
		self.assertIn('1 pessoa(s) presas', saida)
		self.presa.refresh_from_db()
		self.assertEqual(self.presa.responsavel_atual, self.recepcao)

	def test_aplicar_passa_para_o_rodizio_e_registra(self):
		with self.captureOnCommitCallbacks() as callbacks:
			saida = self._rodar('--aplicar')
		self.assertIn('Ana: 1', saida)
		self.assertEqual(callbacks, [])  # sem aviso no WhatsApp
		self.presa.refresh_from_db()
		self.assertEqual(self.presa.responsavel_atual, self.ana)
		self.assertTrue(InteracaoAcolhimento.objects.filter(
			pessoa=self.presa, descricao__contains='Recepcao deixou de ser responsável',
		).exists())
		# Membro (fora do acolhimento) e quem ja esta com alguem que atende ficam como estao.
		self.membro.refresh_from_db()
		self.certa.refresh_from_db()
		self.assertEqual(self.membro.responsavel_atual, self.recepcao)
		self.assertEqual(self.certa.responsavel_atual, self.vol)

	def test_sem_rodizio_fica_sem_responsavel(self):
		PerfilUsuario.objects.filter(usuario=self.ana).update(recebe_novos_contatos=False)
		self.assertIn('sem responsável: 1', self._rodar('--aplicar'))
		self.presa.refresh_from_db()
		self.assertIsNone(self.presa.responsavel_atual)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class AvisoNaTelaDeUsuarioTests(TestCase):
	def test_tirar_a_permissao_de_quem_tem_acolhidos_avisa(self):
		admin = get_user_model().objects.create_superuser('adm_resp', 'adm_resp@ex.com', 'x')
		vol = _usuario('vol', 'pode_conversar_pessoas')
		PrimeiroContato.objects.create(
			nome='Dela', telefone_whatsapp='21911112222', primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			status=Status.EM_ACOMPANHAMENTO, responsavel_atual=vol,
		)
		self.client.force_login(admin)
		resp = self.client.post(reverse('usuarios-editar', args=[vol.pk]), {
			'username': 'vol', 'first_name': 'Vol', 'last_name': '', 'email': '', 'is_active': 'on',
		}, follow=True)
		self.assertContains(resp, 'é responsável por 1 pessoa(s) em acolhimento')
