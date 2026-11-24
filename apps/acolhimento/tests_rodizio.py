"""Rodizio de responsaveis com aviso no WhatsApp do voluntario."""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import rodizio
from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato, TemplateWhatsapp
from apps.acolhimento.tests import SIMPLE_STATIC_STORAGES, _dados_form
from apps.acolhimento.tests_meta import META_OK
from apps.core.models import PerfilUsuario

Status = PrimeiroContato.StatusAcolhimento


def _voluntario(nome, *, rodizio_ligado=True, telefone='(21) 98888-7777', permissao=True, **extra):
	user = get_user_model().objects.create_user(nome, password='x', first_name=nome.title(), **extra)
	if permissao:
		user.user_permissions.add(Permission.objects.get(codename='pode_conversar_pessoas'))
	PerfilUsuario.objects.create(usuario=user, telefone=telefone, recebe_novos_contatos=rodizio_ligado)
	return user


def _pessoa(nome, telefone, **extra):
	return PrimeiroContato.objects.create(
		nome=nome, telefone_whatsapp=telefone, primeira_vez=True,
		como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM, **extra,
	)


class EquipeEVezTests(TestCase):
	def test_so_entra_quem_esta_ligado_ativo_e_com_permissao(self):
		ana = _voluntario('ana')
		_voluntario('fora', rodizio_ligado=False)
		_voluntario('inativo', is_active=False)
		_voluntario('sem_perm', permissao=False)
		admin = _voluntario('admin', permissao=False, is_staff=True)
		self.assertEqual({u.username for u in rodizio.equipe()}, {ana.username, admin.username})

	def test_rodizio_equilibrado(self):
		ana, bia, caio = _voluntario('ana'), _voluntario('bia'), _voluntario('caio')
		pessoas = [_pessoa(f'P{i}', f'2190000000{i}') for i in range(4)]
		with self.captureOnCommitCallbacks():
			recebidos = [rodizio.atribuir(p, 'teste').username for p in pessoas]
		self.assertEqual(recebidos, ['ana', 'bia', 'caio', 'ana'])
		self.assertEqual(PrimeiroContato.objects.get(nome='P3').responsavel_atual, ana)
		# Quem acabou de receber vai para o fim da fila.
		self.assertEqual([u.username for u in rodizio.equipe()], ['bia', 'caio', 'ana'])

	def test_nao_mexe_em_quem_ja_tem_responsavel(self):
		_voluntario('ana')
		dono = _voluntario('dono', rodizio_ligado=False)
		pessoa = _pessoa('Com dono', '21900000001', responsavel_atual=dono)
		self.assertIsNone(rodizio.atribuir(pessoa, 'teste'))
		pessoa.refresh_from_db()
		self.assertEqual(pessoa.responsavel_atual, dono)

	def test_sem_ninguem_no_rodizio_fica_sem_dono(self):
		pessoa = _pessoa('Sem dono', '21900000001')
		self.assertIsNone(rodizio.atribuir(pessoa, 'teste'))
		self.assertIsNone(PrimeiroContato.objects.get(pk=pessoa.pk).responsavel_atual)

	def test_registra_na_timeline_e_agenda_o_aviso(self):
		_voluntario('ana')
		pessoa = _pessoa('Nova', '21900000001')
		with self.captureOnCommitCallbacks() as callbacks:
			rodizio.atribuir(pessoa, 'mandou mensagem no WhatsApp')
		self.assertEqual(len(callbacks), 1)
		passo = InteracaoAcolhimento.objects.get(pessoa=pessoa)
		self.assertIn('Responsável definido pelo rodízio: Ana (mandou mensagem no WhatsApp)', passo.descricao)

	def test_sem_aviso_nao_agenda_nada(self):
		_voluntario('ana')
		with self.captureOnCommitCallbacks() as callbacks:
			rodizio.atribuir(_pessoa('Nova', '21900000001'), 'teste', avisar=False)
		self.assertEqual(callbacks, [])


@override_settings(**META_OK, PUBLIC_BASE_URL='https://acolhimento.exemplo.org')
class AvisoTests(TestCase):
	def setUp(self):
		self.ana = _voluntario('ana')
		self.pessoa = _pessoa('Joana Lima', '21900000001')

	def test_com_template_vai_pelo_template_com_nome_e_link(self):
		TemplateWhatsapp.objects.create(
			tipo=TemplateWhatsapp.Tipo.RESPONSAVEL_ATRIBUIDO, meta_nome='nova_pessoa_atribuida', meta_idioma='pt_BR',
		)
		with patch('apps.acolhimento.meta_service.send_template') as enviar:
			resultado = rodizio.avisar(self.pessoa.pk, self.ana.pk)
		self.assertEqual(resultado, 'Aviso enviado por WhatsApp.')
		kwargs = enviar.call_args.kwargs
		self.assertEqual(kwargs['nome'], 'nova_pessoa_atribuida')
		self.assertEqual(kwargs['to_phone'], '+5521988887777')
		self.assertEqual(kwargs['parametros'], {
			'1': 'Ana',
			'2': 'Joana Lima',
			'3': f'https://acolhimento.exemplo.org/acolhimento/pessoas/{self.pessoa.pk}/mensagens/',
		})

	def test_sem_template_manda_texto_livre(self):
		with patch('apps.acolhimento.meta_service.send_text') as enviar:
			rodizio.avisar(self.pessoa.pk, self.ana.pk)
		texto = enviar.call_args.kwargs['text']
		self.assertIn('Olá, Ana! Joana Lima mandou mensagem', texto)
		self.assertIn(f'/acolhimento/pessoas/{self.pessoa.pk}/mensagens/', texto)

	def test_sem_telefone_nao_tenta(self):
		PerfilUsuario.objects.filter(usuario=self.ana).update(telefone='')
		with patch('apps.acolhimento.meta_service.send_text') as enviar:
			resultado = rodizio.avisar(self.pessoa.pk, self.ana.pk)
		enviar.assert_not_called()
		self.assertIn('sem telefone', resultado)

	def test_falha_da_meta_vira_texto(self):
		with patch('apps.acolhimento.meta_service.send_text', side_effect=RuntimeError('janela fechada')):
			resultado = rodizio.avisar(self.pessoa.pk, self.ana.pk)
		self.assertIn('Aviso não enviado: janela fechada', resultado)


@override_settings(**META_OK, DEBUG=False)
class WebhookRodizioTests(TestCase):
	def setUp(self):
		self.ana = _voluntario('ana')
		for alvo in ('apps.acolhimento.meta_service.marcar_como_lida', 'apps.acolhimento.rodizio._avisar_em_thread'):
			patcher = patch(alvo)
			patcher.start()
			self.addCleanup(patcher.stop)

	def _postar(self, message):
		corpo = json.dumps({
			'object': 'whatsapp_business_account',
			'entry': [{'id': '9876543210', 'changes': [{'field': 'messages', 'value': {
				'messaging_product': 'whatsapp',
				'metadata': {'display_phone_number': '5521900000000', 'phone_number_id': '1234567890'},
				'contacts': [{'profile': {'name': 'Joana'}, 'wa_id': '5521971347985'}],
				'messages': [{'from': '5521971347985', 'id': 'wamid.IN1', 'timestamp': '1700000000', **message}],
			}}]}],
		}).encode('utf-8')
		assinatura = 'sha256=' + hmac.new(b'app-secret-teste', corpo, hashlib.sha256).hexdigest()
		return self.client.post(
			reverse('mensagens-webhook-meta'), data=corpo, content_type='application/json',
			HTTP_X_HUB_SIGNATURE_256=assinatura,
		)

	def test_pessoa_nova_que_escreve_vai_para_o_rodizio(self):
		resp = self._postar({'type': 'text', 'text': {'body': 'oi, quero falar com alguem'}})
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(PrimeiroContato.objects.get().responsavel_atual, self.ana)

	def test_reacao_nao_atribui(self):
		self._postar({'type': 'reaction', 'reaction': {'message_id': 'wamid.X', 'emoji': 'ok'}})
		self.assertIsNone(PrimeiroContato.objects.get().responsavel_atual)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class CadastroTests(TestCase):
	def setUp(self):
		self.ana = _voluntario('ana')

	def test_auto_cadastro_nao_entra_no_rodizio(self):
		# Reclamacao da equipe: quem se cadastrava pelo QR ja caia para alguem antes de
		# interagir. Agora fica sem responsavel ate a IA/menu transferir.
		with self.captureOnCommitCallbacks() as callbacks:
			resp = self.client.post(reverse('auto-cadastro'), _dados_form('21977776666', nome='Pedro'))
		self.assertEqual(resp.status_code, 302)
		self.assertIsNone(PrimeiroContato.objects.get(nome='Pedro Silva').responsavel_atual)
		self.assertEqual(callbacks, [])  # nenhum aviso de rodizio

	def test_cadastro_da_equipe_fica_com_quem_cadastrou(self):
		bia = _voluntario('bia', rodizio_ligado=False)
		self.client.force_login(bia)
		self.client.post(reverse('pessoas-novo'), _dados_form('21977776666', nome='Pedro'))
		self.assertEqual(PrimeiroContato.objects.get(nome='Pedro Silva').responsavel_atual, bia)


class DistribuirAtrasadosTests(TestCase):
	def _escreveu(self, pessoa, dias):
		msg = MensagemContato.objects.create(
			pessoa=pessoa, canal='whatsapp', direcao='entrada', status_fila='enviada', conteudo='oi',
		)
		MensagemContato.objects.filter(pk=msg.pk).update(enfileirada_em=timezone.now() - timedelta(days=dias))

	def test_distribui_so_quem_escreveu_na_semana_e_sem_aviso(self):
		_voluntario('ana')
		_voluntario('bia')
		recente1 = _pessoa('Recente 1', '21900000001')
		recente2 = _pessoa('Recente 2', '21900000002')
		antiga = _pessoa('Antiga', '21900000003')
		membro = _pessoa('Membro', '21900000004', status=Status.MEMBRO)
		self._escreveu(recente1, 1)
		self._escreveu(recente2, 6)
		self._escreveu(antiga, 20)
		self._escreveu(membro, 1)
		with self.captureOnCommitCallbacks() as callbacks:
			resultado = rodizio.distribuir_atrasados()
		self.assertEqual(resultado, {'Ana': 1, 'Bia': 1})
		self.assertEqual(callbacks, [])
		self.assertIsNone(PrimeiroContato.objects.get(pk=antiga.pk).responsavel_atual)
		self.assertIsNone(PrimeiroContato.objects.get(pk=membro.pk).responsavel_atual)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class TelasRodizioTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_superuser('adm_rod', 'adm_rod@ex.com', 'x')
		self.client.force_login(self.admin)

	def _dados_usuario(self, **extra):
		dados = {
			'username': 'nova', 'first_name': 'Nova', 'last_name': '', 'email': '',
			'is_active': 'on', 'password1': 'SenhaForte!123', 'password2': 'SenhaForte!123',
		}
		dados.update(extra)
		return dados

	def test_rodizio_exige_permissao_de_mensagens(self):
		resp = self.client.post(reverse('usuarios-novo'), self._dados_usuario(recebe_novos_contatos='on'))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Para entrar no rodizio')

	def test_cria_usuario_no_rodizio(self):
		resp = self.client.post(reverse('usuarios-novo'), self._dados_usuario(
			recebe_novos_contatos='on', pode_conversar_pessoas='on',
		))
		self.assertEqual(resp.status_code, 302)
		self.assertEqual([u.username for u in rodizio.equipe()], ['nova'])

	def test_lista_mostra_a_fila_e_o_aviso_de_telefone(self):
		_voluntario('ana')
		_voluntario('bia', telefone='')
		resp = self.client.get(reverse('usuarios-lista'))
		self.assertContains(resp, 'Rodízio de novos contatos')
		self.assertContains(resp, 'sem telefone: não recebe aviso')

	def test_distribuir_so_superusuario(self):
		staff = get_user_model().objects.create_user('staff_rod', password='x', is_staff=True)
		self.client.force_login(staff)
		self.assertEqual(self.client.post(reverse('rodizio-distribuir')).status_code, 403)


@override_settings(**META_OK, DEBUG=False)
class IAAntesDoRodizioTests(TestCase):
	"""Com o robo ligado, a pessoa so entra no rodizio quando ele transfere."""

	def setUp(self):
		from apps.acolhimento.models import ConfiguracaoAtendimentoBot, OpcaoAtendimentoBot

		self.ana = _voluntario('ana')
		for alvo in ('apps.acolhimento.meta_service.marcar_como_lida', 'apps.acolhimento.rodizio._avisar_em_thread'):
			patcher = patch(alvo)
			patcher.start()
			self.addCleanup(patcher.stop)
		OpcaoAtendimentoBot.objects.all().delete()  # tira as opcoes padrao da migracao
		OpcaoAtendimentoBot.objects.create(ordem=1, rotulo='Horario dos cultos', palavras_chave='horario', resposta='9h.')
		OpcaoAtendimentoBot.objects.create(
			ordem=2, rotulo='Falar com alguem', palavras_chave='atendente', resposta='Ja chamo alguem.',
			acao=OpcaoAtendimentoBot.Acao.TRANSFERIR_HUMANO,
		)
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.save()

	def _postar(self, message, wamid):
		corpo = json.dumps({
			'object': 'whatsapp_business_account',
			'entry': [{'id': '9876543210', 'changes': [{'field': 'messages', 'value': {
				'messaging_product': 'whatsapp',
				'metadata': {'display_phone_number': '5521900000000', 'phone_number_id': '1234567890'},
				'contacts': [{'profile': {'name': 'Joana'}, 'wa_id': '5521971347985'}],
				'messages': [{'from': '5521971347985', 'id': wamid, 'timestamp': '1700000000', **message}],
			}}]}],
		}).encode('utf-8')
		assinatura = 'sha256=' + hmac.new(b'app-secret-teste', corpo, hashlib.sha256).hexdigest()
		return self.client.post(
			reverse('mensagens-webhook-meta'), data=corpo, content_type='application/json',
			HTTP_X_HUB_SIGNATURE_256=assinatura,
		)

	def _botao(self, texto, wamid):
		return self._postar({'type': 'button', 'button': {'text': texto, 'payload': texto}}, wamid)

	def _pessoa(self):
		return PrimeiroContato.objects.get()

	def test_mensagem_com_robo_ligado_nao_entra_no_rodizio(self):
		self._postar({'type': 'text', 'text': {'body': 'oi'}}, 'wamid.A1')
		self.assertIsNone(self._pessoa().responsavel_atual)

	def test_quando_o_robo_transfere_entra_no_rodizio(self):
		self._postar({'type': 'text', 'text': {'body': 'oi'}}, 'wamid.A1')
		self._postar({'type': 'text', 'text': {'body': '2'}}, 'wamid.A2')  # "Falar com alguem"
		pessoa = self._pessoa()
		self.assertEqual(pessoa.status, Status.EM_ACOMPANHAMENTO)
		self.assertEqual(pessoa.responsavel_atual, self.ana)
		self.assertTrue(InteracaoAcolhimento.objects.filter(
			pessoa=pessoa, descricao__contains='o atendimento automático transferiu: opcao "Falar com alguem"',
		).exists())

	def test_sim_podemos_o_robo_atende_e_nao_entra_no_rodizio(self):
		self._botao('Sim, podemos', 'wamid.B1')
		pessoa = self._pessoa()
		self.assertEqual(pessoa.status, Status.ROBO)
		self.assertIsNone(pessoa.responsavel_atual)
		self.assertTrue(MensagemContato.objects.filter(pessoa=pessoa, direcao='saida').exists())

	def test_sim_podemos_com_ia_vai_para_a_ia_como_primeira_mensagem(self):
		from apps.acolhimento import atendimento_ia
		from apps.acolhimento.models import ConfiguracaoAtendimentoBot

		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ia_ativa = True
		cfg.ia_base_conhecimento = 'Cultos domingo 9h.'
		cfg.save()
		with override_settings(OLLAMA_API_KEY='chave-teste'), patch.object(atendimento_ia, 'agendar') as agendar:
			self._botao('Sim, podemos', 'wamid.B1')
		self.assertTrue(agendar.call_args.kwargs['primeira'])
		self.assertIsNone(self._pessoa().responsavel_atual)

	def test_agora_nao_fica_em_silencio(self):
		self._botao('Agora não', 'wamid.C1')
		pessoa = self._pessoa()
		self.assertFalse(MensagemContato.objects.filter(pessoa=pessoa, direcao='saida').exists())
		self.assertIsNone(pessoa.responsavel_atual)
		self.assertTrue(InteracaoAcolhimento.objects.filter(pessoa=pessoa, descricao__contains='pediu para falar depois').exists())
