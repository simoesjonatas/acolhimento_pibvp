"""O conteudo da mensagem de template mostra o texto que a pessoa recebeu, nao o nome do template."""
import io
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.acolhimento import meta_service, template_config
from apps.acolhimento.models import MensagemContato, PrimeiroContato, TemplateWhatsapp

SIMPLE_STATIC_STORAGES = {
	'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
	'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

META_OK = dict(
	WHATSAPP_PROVIDER='meta',
	META_WA_ACCESS_TOKEN='token-teste',
	META_WA_PHONE_NUMBER_ID='1234567890',
	META_WA_BUSINESS_ACCOUNT_ID='9876543210',
	META_TEMPLATE_OPT_IN_NOME='',
	META_TEMPLATE_CONTINUAR_NOME='',
)

TEMPLATE_BOAS_VINDAS = {
	'name': 'acolhimento_primeiro_contato_equipe',
	'language': 'pt_BR',
	'status': 'APPROVED',
	'components': [
		{'type': 'HEADER', 'format': 'TEXT', 'text': 'PIB Vila da Penha'},
		{'type': 'BODY', 'text': 'Ola {{1}}, que alegria ter voce com a gente!\nPodemos conversar por aqui?'},
		{'type': 'FOOTER', 'text': 'Equipe de acolhimento'},
		{'type': 'BUTTONS', 'buttons': [
			{'type': 'QUICK_REPLY', 'text': 'Sim'},
			{'type': 'QUICK_REPLY', 'text': 'Agora nao'},
		]},
	],
}


def _pessoa(nome='Tayssa Lourenco', telefone='21979382612', **extra):
	dados = dict(
		nome=nome,
		telefone_whatsapp=telefone,
		como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
		status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
	)
	dados.update(extra)
	return PrimeiroContato.objects.create(**dados)


class RenderizarTemplateTests(TestCase):
	def test_monta_cabecalho_corpo_rodape_e_botoes_com_o_nome(self):
		texto = meta_service.renderizar_template(TEMPLATE_BOAS_VINDAS, {'1': 'Maria'})
		self.assertEqual(texto, (
			'*PIB Vila da Penha*\n\n'
			'Ola Maria, que alegria ter voce com a gente!\nPodemos conversar por aqui?\n\n'
			'_Equipe de acolhimento_\n\n'
			'[Sim] | [Agora nao]'
		))

	def test_variaveis_nomeadas_e_sem_valor(self):
		template = {'components': [{'type': 'BODY', 'text': 'Oi {{nome}}, culto as {{ horario }}.'}]}
		self.assertEqual(meta_service.renderizar_template(template, {'nome': 'Ana'}), 'Oi Ana, culto as {{ horario }}.')

	def test_cabecalho_de_midia_e_ignorado(self):
		template = {'components': [
			{'type': 'HEADER', 'format': 'IMAGE'},
			{'type': 'BODY', 'text': 'Corpo'},
		]}
		self.assertEqual(meta_service.renderizar_template(template, {}), 'Corpo')

	def test_cabecalho_de_video_mostra_so_o_texto(self):
		template = {'components': [
			{'type': 'HEADER', 'format': 'VIDEO'},
			{'type': 'BODY', 'text': 'Oi {{1}}, vem pra conferencia!'},
			{'type': 'BUTTONS', 'buttons': [{'type': 'URL', 'text': 'Inscreva-se', 'url': 'https://ex.com'}]},
		]}
		self.assertEqual(
			meta_service.renderizar_template(template, {'1': 'Ana'}),
			'Oi Ana, vem pra conferencia!\n\n[Inscreva-se]',
		)

	def test_sem_corpo_ou_sem_template_devolve_vazio(self):
		self.assertEqual(meta_service.renderizar_template(None, {'1': 'x'}), '')
		self.assertEqual(meta_service.renderizar_template({'components': []}, {}), '')


@override_settings(**META_OK)
class TextoMetaTemplateTests(TestCase):
	def setUp(self):
		cache.clear()

	def test_consulta_a_meta_uma_vez_e_usa_o_cache(self):
		bloco = {'nome': 'acolhimento_primeiro_contato_equipe', 'idioma': 'pt_BR', 'parametros': {'1': 'Ana'}}
		with patch.object(meta_service, 'obter_template', return_value=TEMPLATE_BOAS_VINDAS) as get:
			self.assertIn('Ola Ana,', template_config.texto_meta_template(bloco))
			self.assertIn('Ola Bia,', template_config.texto_meta_template({**bloco, 'parametros': {'1': 'Bia'}}))
		get.assert_called_once()

	def test_sem_template_usa_o_marcador(self):
		with patch.object(meta_service, 'obter_template', return_value=None):
			self.assertEqual(
				template_config.conteudo_template({'nome': 'x', 'idioma': 'pt_BR'}, 'Template opt-in enfileirado'),
				'Template opt-in enfileirado',
			)


@override_settings(**META_OK, STORAGES=SIMPLE_STATIC_STORAGES)
class EnfileirarComTextoTests(TestCase):
	def setUp(self):
		cache.clear()
		for alvo, valor in (('get_phone_number_info', {}), ('obter_template', TEMPLATE_BOAS_VINDAS)):
			p = patch(f'apps.acolhimento.meta_service.{alvo}', return_value=valor)
			p.start()
			self.addCleanup(p.stop)
		self.user = get_user_model().objects.create_user('equipe_texto', password='x', is_staff=True)
		self.client.force_login(self.user)
		self.pessoa = _pessoa()

	def test_opt_in_grava_o_texto_que_a_pessoa_recebe(self):
		TemplateWhatsapp.objects.create(
			tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, meta_nome='acolhimento_primeiro_contato_equipe',
		)
		self.client.post(reverse('dashboard'), {'action': 'disparar_template_opt_in'})
		mensagem = MensagemContato.objects.get(pessoa=self.pessoa)
		self.assertIn('Ola Tayssa Lourenco, que alegria', mensagem.conteudo)
		self.assertNotIn('enfileirado', mensagem.conteudo)
		# O que vai para a Meta continua sendo o template (nome + variaveis).
		self.assertEqual(mensagem.metadata_envio['meta_template']['nome'], 'acolhimento_primeiro_contato_equipe')

	def test_continuar_grava_o_texto(self):
		TemplateWhatsapp.objects.create(tipo=TemplateWhatsapp.Tipo.CONTINUAR, meta_nome='acolhimento_continuar_conversa')
		self.pessoa.iniciou_interacao = True
		self.pessoa.save()
		self.client.post(reverse('pessoas-template-continuar', args=[self.pessoa.pk]))
		mensagem = MensagemContato.objects.get(pessoa=self.pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA)
		self.assertIn('Ola Tayssa Lourenco,', mensagem.conteudo)

	def test_disparo_massa_grava_o_texto(self):
		self.pessoa.status = PrimeiroContato.StatusAcolhimento.PARTICIPANTE
		self.pessoa.save()
		self.client.post(reverse('mensagens-disparo-massa'), {
			'modo': 'template', 'content_sid': 'convite_evento', 'template_idioma': 'pt_BR',
			'content_variables': '{"1": "{nome}"}', 'pessoas': [self.pessoa.pk],
		})
		mensagem = MensagemContato.objects.get(pessoa=self.pessoa)
		self.assertIn('Ola Tayssa Lourenco,', mensagem.conteudo)
		self.assertNotIn('[Template]', mensagem.conteudo)

	def _mensagem_template(self, conteudo='Ola Tayssa, que alegria'):
		return MensagemContato.objects.create(
			pessoa=self.pessoa, canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA, status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo=conteudo,
			metadata_envio={'meta_template': {'nome': 'acolhimento_primeiro_contato_equipe', 'idioma': 'pt_BR'}},
		)

	def test_fila_e_conversa_mostram_o_modelo_usado(self):
		self._mensagem_template()
		resp = self.client.get(reverse('mensagens-fila'))
		self.assertContains(resp, 'Ola Tayssa, que alegria')
		self.assertContains(resp, 'Modelo acolhimento_primeiro_contato_equipe')
		resp = self.client.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		self.assertContains(resp, 'Enviada pelo modelo aprovado <strong>acolhimento_primeiro_contato_equipe</strong>', html=False)


@override_settings(**META_OK)
class PreencherTextoTemplatesCommandTests(TestCase):
	def setUp(self):
		cache.clear()
		self.pessoa = _pessoa()

	def _mensagem(self, conteudo, metadata):
		return MensagemContato.objects.create(
			pessoa=self.pessoa, canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA, status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo=conteudo, metadata_envio=metadata,
		)

	def test_troca_so_os_marcadores_de_template_da_meta(self):
		bloco = {'nome': 'acolhimento_primeiro_contato_equipe', 'idioma': 'pt_BR', 'parametros': {'1': 'Tayssa'}}
		antiga = self._mensagem('Template opt-in enfileirado', {'meta_template': bloco})
		massa = self._mensagem('[Template] acolhimento_primeiro_contato_equipe', {'meta_template': bloco})
		livre = self._mensagem('Oi, tudo bem?', {'meta_template': bloco})
		twilio = self._mensagem('Template opt-in enfileirado', {'twilio_template': {'content_sid': 'HX1'}})
		falhou = self._mensagem('[Template] acolhimento_primeiro_contato_equipe', {'meta_template': bloco})
		MensagemContato.objects.filter(pk=falhou.pk).update(status_fila=MensagemContato.StatusFilaChoices.FALHA)

		saida = io.StringIO()
		with patch.object(meta_service, 'obter_template', return_value=TEMPLATE_BOAS_VINDAS):
			call_command('preencher_texto_templates', '--dry-run', stdout=saida)
			antiga.refresh_from_db()
			self.assertEqual(antiga.conteudo, 'Template opt-in enfileirado')
			self.assertIn('2 mensagem(ns) seriam atualizadas', saida.getvalue())

			call_command('preencher_texto_templates', stdout=io.StringIO())

		for mensagem in (antiga, massa, livre, twilio, falhou):
			mensagem.refresh_from_db()
		self.assertEqual(falhou.conteudo, '[Template] acolhimento_primeiro_contato_equipe')
		self.assertIn('Ola Tayssa, que alegria', antiga.conteudo)
		self.assertIn('Ola Tayssa, que alegria', massa.conteudo)
		self.assertEqual(livre.conteudo, 'Oi, tudo bem?')
		self.assertEqual(twilio.conteudo, 'Template opt-in enfileirado')

	def test_sem_acesso_ao_template_avisa_e_nao_muda(self):
		msg = self._mensagem('Template opt-in enfileirado', {'meta_template': {'nome': 'x', 'idioma': 'pt_BR'}})
		saida = io.StringIO()
		with patch.object(meta_service, 'obter_template', return_value=None):
			call_command('preencher_texto_templates', stdout=saida)
		msg.refresh_from_db()
		self.assertEqual(msg.conteudo, 'Template opt-in enfileirado')
		self.assertIn('sem texto', saida.getvalue())
