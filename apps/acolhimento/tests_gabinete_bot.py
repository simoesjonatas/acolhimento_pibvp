"""Testes do gabinete (pastor + avisos), filtros novos, responsavel e midia no bot. Sem rede."""
import json
import shutil
import tempfile
from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import atendimento_bot, bot_midia, gabinete_avisos, meta_service, whatsapp_gateway
from apps.acolhimento.forms import OpcaoAtendimentoBotForm
from apps.acolhimento.models import (
	ConfiguracaoAtendimentoBot,
	MensagemContato,
	OpcaoAtendimentoBot,
	OpcaoComoConheceu,
	PrimeiroContato,
	RegistroGabinete,
	TemplateWhatsapp,
)
from apps.acolhimento.tests_meta import META_OK, SIMPLE_STATIC_STORAGES, _pessoa
from apps.core.models import salvar_telefone_do_usuario

User = get_user_model()


def _usuario(nome, *perms, **extra):
	usuario = User.objects.create_user(nome, password='x', **extra)
	for codename in perms:
		usuario.user_permissions.add(Permission.objects.get(codename=codename))
	return usuario


def _abrir_janela(pessoa):
	pessoa.iniciou_interacao = True
	pessoa.save(update_fields=['iniciou_interacao'])
	MensagemContato.objects.create(
		pessoa=pessoa,
		direcao=MensagemContato.DirecaoChoices.ENTRADA,
		status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
		conteudo='oi',
		enviada_em=timezone.now(),
	)


class _PastaMidia:
	def setUp(self):
		super().setUp()
		self.pasta = tempfile.mkdtemp(prefix='bot-midia-')
		self.addCleanup(shutil.rmtree, self.pasta, ignore_errors=True)
		override = override_settings(MIDIA_PRIVADA_ROOT=self.pasta)
		override.enable()
		self.addCleanup(override.disable)


# ---------------------------------------------------------------------------
# Gabinete: pastor + avisos
# ---------------------------------------------------------------------------
@override_settings(**META_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class GabineteAgendadoTests(TestCase):
	def setUp(self):
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.pastor = _usuario('pastor_jose', 'pode_ver_gabinete', first_name='José', last_name='Pastor')
		self.pessoa = _pessoa(nome='Marta')
		self.url = reverse('pessoas-gabinete', args=[self.pessoa.pk])
		self.client.force_login(self.pastor)

	def _agendar(self, **extra):
		dados = {
			'gabinete-tipo': 'agendado',
			'gabinete-pastor': self.pastor.pk,
			'gabinete-data': '2026-12-01',
			'gabinete-hora': '19:30',
		}
		dados.update(extra)
		return self.client.post(self.url, dados)

	def test_agendado_exige_pastor_e_realizado_nao(self):
		self._agendar(**{'gabinete-pastor': ''})
		self.assertFalse(RegistroGabinete.objects.exists())

		self._agendar(**{'gabinete-pastor': '', 'gabinete-tipo': 'realizado'})
		self.assertEqual(RegistroGabinete.objects.get().tipo, 'realizado')
		# Realizado nao dispara nenhum aviso.
		self.assertFalse(MensagemContato.objects.filter(direcao='saida').exists())

	def test_so_pastores_aparecem_no_select_e_pastor_logado_vem_marcado(self):
		_usuario('equipe_x', 'pode_conversar_pessoas')
		resp = self.client.get(reverse('pessoas-detalhe', args=[self.pessoa.pk]))
		campo = resp.context['gabinete_form'].fields['pastor']
		self.assertEqual(list(campo.queryset), [self.pastor])
		self.assertEqual(resp.context['gabinete_form']['pastor'].value(), self.pastor.pk)

	def test_janela_aberta_enfileira_texto_para_a_pessoa(self):
		_abrir_janela(self.pessoa)
		self._agendar()

		registro = RegistroGabinete.objects.get()
		self.assertEqual(registro.pastor, self.pastor)
		saida = MensagemContato.objects.get(direcao='saida')
		self.assertIn('01/12/2026', saida.conteudo)
		self.assertIn('19:30', saida.conteudo)
		self.assertIn('José Pastor', saida.conteudo)
		self.assertNotIn('tipo_template', saida.metadata_envio)
		self.assertIn('enfileirada', registro.aviso_pessoa)
		# Pastor sem telefone no perfil: avisa na tela, sem tentar enviar.
		self.assertIn('sem telefone', registro.aviso_pastor)

	def test_janela_fechada_sem_template_nao_envia_e_explica(self):
		self._agendar()
		registro = RegistroGabinete.objects.get()
		self.assertFalse(MensagemContato.objects.filter(direcao='saida').exists())
		self.assertIn('fora da janela', registro.aviso_pessoa)

	def test_janela_fechada_usa_template_da_meta_com_variaveis(self):
		TemplateWhatsapp.objects.create(
			tipo=TemplateWhatsapp.Tipo.GABINETE_PESSOA, meta_nome='gabinete_agendado', meta_idioma='pt_BR',
		)
		self._agendar()
		saida = MensagemContato.objects.get(direcao='saida')
		template = saida.metadata_envio['meta_template']
		self.assertEqual(template['nome'], 'gabinete_agendado')
		self.assertEqual(
			template['parametros'],
			{'1': 'Marta', '2': '01/12/2026', '3': '19:30', '4': 'José Pastor'},
		)
		self.assertEqual(saida.metadata_envio['tipo_template'], TemplateWhatsapp.Tipo.GABINETE_PESSOA)

	def test_pastor_com_telefone_recebe_aviso_depois_do_commit(self):
		salvar_telefone_do_usuario(self.pastor, '(21) 99999-0000')
		with patch('apps.acolhimento.gabinete_avisos.threading.Thread') as thread, \
				self.captureOnCommitCallbacks(execute=True):
			self._agendar()
		thread.assert_called_once()
		self.assertIn('sendo enviado', RegistroGabinete.objects.get().aviso_pastor)

	def test_enviar_aviso_pastor_usa_template_ou_texto_e_grava_falha(self):
		salvar_telefone_do_usuario(self.pastor, '(21) 99999-0000')
		registro = RegistroGabinete.objects.create(
			pessoa=self.pessoa, tipo='agendado', pastor=self.pastor, data=date(2026, 12, 1), hora=time(19, 30),
		)
		with patch('apps.acolhimento.meta_service.send_text', return_value={'sid': 'x'}) as texto:
			self.assertEqual(gabinete_avisos.enviar_aviso_pastor(registro.pk), 'Aviso enviado ao pastor.')
		self.assertEqual(texto.call_args.kwargs['to_phone'], '+5521999990000')
		self.assertIn('Marta', texto.call_args.kwargs['text'])

		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.GABINETE_PASTOR, meta_nome='aviso_pastor')
		with patch('apps.acolhimento.meta_service.send_template', return_value={'sid': 'x'}) as tpl:
			gabinete_avisos.enviar_aviso_pastor(registro.pk)
		self.assertEqual(tpl.call_args.kwargs['parametros'], {'1': 'José Pastor', '2': 'Marta', '3': '01/12/2026', '4': '19:30'})

		with patch('apps.acolhimento.meta_service.send_template', side_effect=meta_service.MetaWhatsAppError('fora')):
			gabinete_avisos.enviar_aviso_pastor(registro.pk)
		self.assertIn('falhou', RegistroGabinete.objects.get(pk=registro.pk).aviso_pastor)


# ---------------------------------------------------------------------------
# Filtro por pastor, tela inicial e responsavel
# ---------------------------------------------------------------------------
@override_settings(**META_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class PastorFiltroPainelTests(TestCase):
	def setUp(self):
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.pastor = _usuario('pastor_a', 'pode_ver_gabinete', first_name='Paulo')
		self.outro_pastor = _usuario('pastor_b', 'pode_ver_gabinete', first_name='Pedro')
		self.equipe = _usuario('equipe_a', 'pode_conversar_pessoas')
		self.ana = _pessoa(nome='Ana', telefone='21911110001')
		self.bia = _pessoa(nome='Bia', telefone='21911110002')
		hoje = timezone.localdate()
		RegistroGabinete.objects.create(pessoa=self.ana, tipo='agendado', pastor=self.pastor, data=hoje + timedelta(days=2))
		RegistroGabinete.objects.create(pessoa=self.bia, tipo='realizado', pastor=self.outro_pastor, data=hoje)

	def test_filtro_pastor_na_lista(self):
		self.client.force_login(self.pastor)
		resp = self.client.get(reverse('pessoas-lista'), {'pastor': self.pastor.pk})
		self.assertEqual([p.nome for p in resp.context['pessoas']], ['Ana'])
		self.assertContains(resp, 'Pastor do gabinete')

	def test_filtro_pastor_ignorado_para_quem_nao_ve_gabinete(self):
		PrimeiroContato.objects.filter(pk__in=[self.ana.pk, self.bia.pk]).update(responsavel_atual=self.equipe)
		self.client.force_login(self.equipe)
		resp = self.client.get(reverse('pessoas-lista'), {'pastor': self.pastor.pk})
		self.assertEqual(resp.context['total_filtrado'], 2)
		self.assertNotContains(resp, 'Pastor do gabinete')

	def test_painel_mostra_pessoas_do_pastor(self):
		self.client.force_login(self.pastor)
		resp = self.client.get(reverse('dashboard'))
		self.assertContains(resp, 'Meus gabinetes')
		self.assertEqual([item['pessoa'].nome for item in resp.context['gabinetes_do_pastor']], ['Ana'])
		self.assertIsNotNone(resp.context['gabinetes_do_pastor'][0]['proximo'])

		self.client.force_login(self.equipe)
		self.assertNotContains(self.client.get(reverse('dashboard')), 'Meus gabinetes')

	def test_responsavel_so_lista_mensagens_e_pastores(self):
		admin = _usuario('admin_r', is_staff=True)
		_usuario('sem_perfil')
		self.client.force_login(admin)
		resp = self.client.get(reverse('pessoas-detalhe', args=[self.ana.pk]))
		nomes = {u.username for u in resp.context['usuarios_responsaveis']}
		self.assertEqual(nomes, {'pastor_a', 'pastor_b', 'equipe_a'})

		sem_perfil = User.objects.get(username='sem_perfil')
		self.client.post(reverse('pessoas-responsavel', args=[self.ana.pk]), {'responsavel_atual': sem_perfil.pk})
		self.ana.refresh_from_db()
		self.assertIsNone(self.ana.responsavel_atual)

		self.client.post(reverse('pessoas-responsavel', args=[self.ana.pk]), {'responsavel_atual': self.pastor.pk})
		self.ana.refresh_from_db()
		self.assertEqual(self.ana.responsavel_atual, self.pastor)

	def test_responsavel_atual_sem_perfil_continua_na_lista(self):
		admin = _usuario('admin_r', is_staff=True)
		antigo = _usuario('antigo')
		self.ana.responsavel_atual = antigo
		self.ana.save(update_fields=['responsavel_atual'])
		self.client.force_login(admin)
		resp = self.client.get(reverse('pessoas-detalhe', args=[self.ana.pk]))
		self.assertIn(antigo, list(resp.context['usuarios_responsaveis']))


# ---------------------------------------------------------------------------
# Como conheceu: lista de pessoas e disparo em massa
# ---------------------------------------------------------------------------
@override_settings(**META_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class ComoConheceuFiltroTests(TestCase):
	def setUp(self):
		indicador = patch('apps.acolhimento.meta_service.get_phone_number_info', return_value={})
		indicador.start()
		self.addCleanup(indicador.stop)
		self.admin = _usuario('admin_cc', is_staff=True, is_superuser=True)
		self.client.force_login(self.admin)
		OpcaoComoConheceu.objects.get_or_create(valor='instagram', defaults={'rotulo': 'Instagram'})
		OpcaoComoConheceu.objects.get_or_create(valor='amigo', defaults={'rotulo': 'Amigo'})
		Status = PrimeiroContato.StatusAcolhimento
		self.insta_novo = _pessoa(nome='Insta Novo', telefone='21922220001', como_conheceu='instagram')
		self.insta_acomp = _pessoa(
			nome='Insta Acomp', telefone='21922220002', como_conheceu='instagram', status=Status.EM_ACOMPANHAMENTO,
		)
		self.amigo_acomp = _pessoa(
			nome='Amigo Acomp', telefone='21922220003', como_conheceu='amigo', status=Status.EM_ACOMPANHAMENTO,
		)

	def test_lista_de_pessoas_filtra_como_conheceu(self):
		resp = self.client.get(reverse('pessoas-lista'), {'como_conheceu': 'instagram'})
		self.assertEqual({p.nome for p in resp.context['pessoas']}, {'Insta Novo', 'Insta Acomp'})
		self.assertContains(resp, 'Como conheceu')

	def _audiencia(self, **params):
		resp = self.client.get(reverse('mensagens-disparo-massa'), params)
		return {item['pessoa'].nome for item in resp.context['audiencia']}

	def test_disparo_filtra_como_conheceu_e_primeiro_contato_sob_demanda(self):
		self.assertEqual(self._audiencia(), {'Insta Acomp', 'Amigo Acomp'})
		self.assertEqual(self._audiencia(como_conheceu='instagram'), {'Insta Acomp'})
		self.assertEqual(self._audiencia(status='primeiro_contato'), {'Insta Novo'})
		self.assertEqual(self._audiencia(status='primeiro_contato', como_conheceu='amigo'), set())

	def test_disparo_template_para_primeiro_contato_filtrado(self):
		url = reverse('mensagens-disparo-massa') + '?status=primeiro_contato'
		resp = self.client.post(url, {
			'modo': 'template',
			'content_sid': 'convite_culto',
			'template_idioma': 'pt_BR',
			'content_variables': '',
			'pessoas': [self.insta_novo.pk],
		})
		self.assertEqual(resp.status_code, 302)
		self.assertTrue(MensagemContato.objects.filter(pessoa=self.insta_novo, direcao='saida').exists())


# ---------------------------------------------------------------------------
# Bot respondendo com midia
# ---------------------------------------------------------------------------
@override_settings(**META_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class BotMidiaTests(_PastaMidia, TestCase):
	def _opcao(self, nome='cartaz.jpg', conteudo=b'\xff\xd8\xff', **extra):
		form = OpcaoAtendimentoBotForm(
			data={'rotulo': 'Agenda', 'palavras_chave': 'agenda', 'resposta': 'Segue a agenda:',
				'acao': 'responder', 'ativa': 'on', 'aparece_no_menu': 'on', **extra},
			files={'midia_arquivo': SimpleUploadedFile(nome, conteudo)},
		)
		self.assertTrue(form.is_valid(), form.errors)
		return form.save()

	def test_form_aceita_imagem_e_recusa_formato_ou_tamanho(self):
		opcao = self._opcao()
		self.assertEqual((opcao.midia_tipo, opcao.midia_mime, opcao.midia_nome_original), ('image', 'image/jpeg', 'cartaz.jpg'))
		self.assertTrue(opcao.midia_arquivo.path.startswith(self.pasta))

		dados = {'rotulo': 'X', 'resposta': 'y', 'acao': 'responder', 'ativa': 'on', 'aparece_no_menu': 'on'}
		form = OpcaoAtendimentoBotForm(data=dados, files={'midia_arquivo': SimpleUploadedFile('virus.exe', b'MZ')})
		self.assertIn('midia_arquivo', form.errors)
		grande = SimpleUploadedFile('foto.png', b'0' * (5 * 1024 * 1024 + 1))
		form = OpcaoAtendimentoBotForm(data=dados, files={'midia_arquivo': grande})
		self.assertIn('midia_arquivo', form.errors)

	def test_remover_apaga_arquivo(self):
		opcao = self._opcao()
		caminho = opcao.midia_arquivo.path
		form = OpcaoAtendimentoBotForm(
			instance=opcao,
			data={'rotulo': 'Agenda', 'palavras_chave': 'agenda', 'resposta': 'Segue', 'acao': 'responder',
				'ativa': 'on', 'aparece_no_menu': 'on', 'remover_midia': 'on'},
		)
		self.assertTrue(form.is_valid(), form.errors)
		opcao = form.save()
		self.assertFalse(opcao.midia_arquivo)
		self.assertEqual(opcao.midia_tipo, '')
		import os
		self.assertFalse(os.path.exists(caminho))

	def _bot_responde(self, opcao):
		ConfiguracaoAtendimentoBot.objects.update_or_create(pk=1, defaults={'ativo': True})
		pessoa = _pessoa(bot_etapa=PrimeiroContato.BotEtapaChoices.MENU, status=PrimeiroContato.StatusAcolhimento.ROBO)
		_abrir_janela(pessoa)
		self.assertTrue(atendimento_bot.processar_entrada(pessoa, 'agenda'))
		return pessoa, list(MensagemContato.objects.filter(pessoa=pessoa, direcao='saida').order_by('id'))

	def test_bot_enfileira_texto_e_depois_o_arquivo(self):
		opcao = self._opcao()
		_pessoa_, saidas = self._bot_responde(opcao)
		self.assertEqual([m.conteudo for m in saidas], ['Segue a agenda:', '(imagem)'])
		midia = saidas[1]
		self.assertEqual(midia.midia_tipo, 'image')
		self.assertEqual(midia.metadata_envio['bot_midia_opcao_id'], opcao.pk)
		self.assertEqual(midia.midia_url, reverse('atendimento-opcao-midia', args=[opcao.pk]))
		self.assertIsNone(whatsapp_gateway.motivo_bloqueio_mensagem(midia))

	@override_settings(WHATSAPP_PROVIDER='evolution')
	def test_fora_da_meta_so_vai_o_texto(self):
		opcao = self._opcao()
		_pessoa_, saidas = self._bot_responde(opcao)
		self.assertEqual([m.conteudo for m in saidas], ['Segue a agenda:'])

	def test_gateway_envia_arquivo_e_reaproveita_id(self):
		opcao = self._opcao()
		_pessoa_, saidas = self._bot_responde(opcao)
		with patch('apps.acolhimento.meta_service.enviar_arquivo_para_meta', return_value='MID1') as upload, \
				patch('apps.acolhimento.meta_service.send_media', return_value={'sid': 'wamid.M', 'status': 'accepted'}) as envio:
			resultado = whatsapp_gateway.enviar(saidas[1], '+5521971347985')
			whatsapp_gateway.enviar(saidas[1], '+5521971347985')

		upload.assert_called_once()
		self.assertEqual(upload.call_args.kwargs['mime'], 'image/jpeg')
		self.assertEqual(envio.call_args.kwargs['tipo'], 'image')
		self.assertEqual(envio.call_args.kwargs['media_id'], 'MID1')
		self.assertEqual(resultado['referencia_externa'], 'wamid.M')

	def test_arquivo_removido_vira_falha_de_envio(self):
		opcao = self._opcao()
		_pessoa_, saidas = self._bot_responde(opcao)
		OpcaoAtendimentoBot.objects.filter(pk=opcao.pk).update(midia_arquivo='')
		with self.assertRaises(whatsapp_gateway.WhatsAppSendError):
			whatsapp_gateway.enviar(saidas[1], '+5521971347985')

	def test_view_do_arquivo_exige_permissao_da_conversa(self):
		opcao = self._opcao()
		url = reverse('atendimento-opcao-midia', args=[opcao.pk])
		self.assertEqual(self.client.get(url).status_code, 403)
		self.client.force_login(_usuario('sem_perm_bot'))
		self.assertEqual(self.client.get(url).status_code, 403)
		self.client.force_login(_usuario('com_perm_bot', 'pode_conversar_pessoas'))
		resp = self.client.get(url)
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(b''.join(resp.streaming_content), b'\xff\xd8\xff')


@override_settings(**META_OK)
class MetaServiceEnvioMidiaTests(TestCase):
	def test_upload_multipart_e_send_media(self):
		capturado = []

		class _Resp:
			def __init__(self, body):
				self._body = json.dumps(body).encode()
				self.status = 200

			def read(self, *a):
				return self._body

			def __enter__(self):
				return self

			def __exit__(self, *a):
				return False

		def fake_urlopen(req, timeout=None):
			capturado.append(req)
			if req.full_url.endswith('/media'):
				return _Resp({'id': 'MID9'})
			return _Resp({'messages': [{'id': 'wamid.D'}], 'contacts': [{'wa_id': '5521971347985'}]})

		with patch('apps.acolhimento.meta_service.urllib.request.urlopen', side_effect=fake_urlopen):
			media_id = meta_service.enviar_arquivo_para_meta(b'%PDF-1', mime='application/pdf', nome='agenda.pdf')
			meta_service.send_media(to_phone='21971347985', tipo='document', media_id=media_id, nome_arquivo='agenda.pdf')

		upload, envio = capturado
		self.assertTrue(upload.full_url.endswith('/1234567890/media'))
		self.assertIn('multipart/form-data; boundary=', upload.get_header('Content-type'))
		self.assertIn(b'name="messaging_product"\r\n\r\nwhatsapp', upload.data)
		self.assertIn(b'filename="agenda.pdf"', upload.data)
		self.assertIn(b'%PDF-1', upload.data)
		corpo = json.loads(envio.data)
		self.assertEqual(corpo['type'], 'document')
		self.assertEqual(corpo['document'], {'id': 'MID9', 'filename': 'agenda.pdf'})

	def test_audio_nao_leva_legenda(self):
		with patch('apps.acolhimento.meta_service._post_messages', return_value={}) as post:
			meta_service.send_media(to_phone='21971347985', tipo='audio', media_id='A', legenda='oi')
		self.assertEqual(post.call_args.args[0]['audio'], {'id': 'A'})
