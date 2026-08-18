from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from apps.acolhimento import atendimento_bot
from apps.acolhimento import evolution_service, fila_auto, mensagem_variacao, whatsapp_gateway
from apps.acolhimento.fila_processor import processar_fila_mensagens
from apps.acolhimento.forms import AutoCadastroPrimeiroContatoForm, PerguntaForm, PrimeiroContatoForm, RelatorioPessoasForm, ResponderQuestionarioForm
from apps.acolhimento.models import (
	ConfiguracaoAtendimentoBot,
	ConfiguracaoProcessamentoFila,
	ConviteQuestionario,
	InteracaoAcolhimento,
	MensagemContato,
	OpcaoAtendimentoBot,
	OpcaoPergunta,
	PerguntaQuestionario,
	PrimeiroContato,
	Questionario,
	TemplateWhatsapp,
)
from apps.acolhimento.views import (
	PERMISSAO_CONVERSAR_PESSOAS,
	ConfiguracaoTemplatesView,
	DisparoMensagemMassaView,
	ExcluirConviteQuestionarioView,
	MensagemFilaListView,
	PrimeiroContatoMensagensView,
	ProcessamentoFilaControleView,
	QuestionarioListView,
	RelatorioPessoasView,
)
from apps.core.views import UsuarioListView


SIMPLE_STATIC_STORAGES = {
	'default': {
		'BACKEND': 'django.core.files.storage.FileSystemStorage',
	},
	'staticfiles': {
		'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
	},
}


def _dados_form(telefone, nome='Fulano'):
	return {
		'nome': nome,
		'telefone_whatsapp': telefone,
		'primeira_vez': 'True',
		'como_conheceu': PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		'o_que_busca': PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
	}


class TelefoneWhatsappUnicoTests(TestCase):
	def setUp(self):
		self.pessoa = PrimeiroContato.objects.create(
			nome='Maria',
			telefone_whatsapp='31999999999',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
		)

	def test_bloqueia_numero_identico(self):
		form = PrimeiroContatoForm(data=_dados_form('31999999999'))
		self.assertFalse(form.is_valid())
		self.assertIn('telefone_whatsapp', form.errors)

	def test_bloqueia_numero_com_mascara_diferente(self):
		form = PrimeiroContatoForm(data=_dados_form('(31) 99999-9999'))
		self.assertFalse(form.is_valid())
		self.assertIn('telefone_whatsapp', form.errors)

	def test_bloqueia_numero_com_ddi_55(self):
		form = PrimeiroContatoForm(data=_dados_form('5531999999999'))
		self.assertFalse(form.is_valid())
		self.assertIn('telefone_whatsapp', form.errors)

	def test_bloqueia_numero_sem_nono_digito(self):
		form = PrimeiroContatoForm(data=_dados_form('3199999999'))
		self.assertFalse(form.is_valid())
		self.assertIn('telefone_whatsapp', form.errors)

	def test_permite_numero_diferente(self):
		form = PrimeiroContatoForm(data=_dados_form('31988888888'))
		self.assertTrue(form.is_valid(), form.errors)

	def test_permite_editar_a_propria_pessoa(self):
		form = PrimeiroContatoForm(data=_dados_form('31999999999', nome='Maria Atualizada'), instance=self.pessoa)
		self.assertTrue(form.is_valid(), form.errors)

	def test_team_form_revela_nome_do_duplicado(self):
		form = PrimeiroContatoForm(data=_dados_form('31999999999'))
		self.assertFalse(form.is_valid())
		self.assertIn('Maria', form.errors['telefone_whatsapp'][0])

	def test_auto_cadastro_nao_revela_nome_do_duplicado(self):
		form = AutoCadastroPrimeiroContatoForm(data=_dados_form('31999999999'))
		self.assertFalse(form.is_valid())
		self.assertNotIn('Maria', form.errors['telefone_whatsapp'][0])

	def test_auto_cadastro_sem_como_conheceu_e_o_que_busca(self):
		dados = _dados_form('31988888888')
		dados.pop('como_conheceu')
		dados.pop('o_que_busca')
		form = AutoCadastroPrimeiroContatoForm(data=dados)
		self.assertTrue(form.is_valid(), form.errors)
		pessoa = form.save()
		self.assertEqual(pessoa.como_conheceu, '')
		self.assertEqual(pessoa.o_que_busca, '')


class StatusEvolutivoTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user(username='equipe', password='x', is_staff=True)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Joao',
			telefone_whatsapp='31977777777',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO,
		)
		self.client.force_login(self.user)

	def test_participante_vem_antes_de_membro(self):
		valores = list(PrimeiroContato.StatusAcolhimento.values)
		self.assertIn('participante', valores)
		self.assertLess(valores.index('participante'), valores.index('membro'))

	def test_troca_status_valida_e_registra_timeline(self):
		url = reverse('pessoas-status', args=[self.pessoa.pk])
		resp = self.client.post(url, {'status': 'participante'})
		# fetch_redirect_response=False evita renderizar o alvo (incompat Django 4.2 + Python 3.14).
		self.assertRedirects(
			resp,
			reverse('pessoas-detalhe', args=[self.pessoa.pk]),
			fetch_redirect_response=False,
		)
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.PARTICIPANTE)
		self.assertTrue(
			self.pessoa.interacoes.filter(tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO).exists()
		)

	def test_troca_status_invalida_nao_altera(self):
		url = reverse('pessoas-status', args=[self.pessoa.pk])
		self.client.post(url, {'status': 'inexistente'})
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_timeline_renderiza_hora_e_filtra_eventos(self):
		InteracaoAcolhimento.objects.create(
			pessoa=self.pessoa,
			tipo=InteracaoAcolhimento.TipoInteracao.VISITA_AGENDADA,
			descricao='Cafe agendado',
			data_interacao='2026-07-24',
			hora_interacao=time(15, 45),
		)
		InteracaoAcolhimento.objects.create(
			pessoa=self.pessoa,
			tipo=InteracaoAcolhimento.TipoInteracao.TENTATIVA_CONTATO,
			descricao='Mensagem inicial',
			data_interacao='2026-07-23',
			hora_interacao=time(9, 10),
		)

		resp = self.client.get(
			reverse('pessoas-detalhe', args=[self.pessoa.pk]),
			{
				'timeline_q': 'Cafe',
				'timeline_tipo': InteracaoAcolhimento.TipoInteracao.VISITA_AGENDADA,
			},
		)

		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, '15:45')
		self.assertContains(resp, 'Cafe agendado')
		self.assertNotContains(resp, 'Mensagem inicial')

	def test_novo_evento_timeline_salva_data_e_hora(self):
		resp = self.client.post(
			reverse('pessoas-detalhe', args=[self.pessoa.pk]),
			{
				'tipo': InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
				'data_interacao': '2026-07-24',
				'hora_interacao': '16:20',
				'descricao': 'Conversa registrada com horario.',
			},
		)

		self.assertEqual(resp.status_code, 302)
		interacao = self.pessoa.interacoes.get(descricao='Conversa registrada com horario.')
		self.assertEqual(interacao.hora_interacao, time(16, 20))


class BloqueioWhatsappOptInTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user(username='equipe', password='x', is_staff=True)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Visitante',
			telefone_whatsapp='31955554444',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.ROBO,
			iniciou_interacao=False,
		)
		self.client.force_login(self.user)

	def test_bloqueia_mensagem_whatsapp_manual_sem_resposta_ao_template(self):
		url = reverse('pessoas-enfileirar-mensagem', args=[self.pessoa.pk])
		resp = self.client.post(
			url,
			{
				'canal': MensagemContato.CanalChoices.WHATSAPP,
				'conteudo': 'Oi, tudo bem?',
			},
		)

		self.assertEqual(resp.status_code, 302)
		self.assertFalse(
			MensagemContato.objects.filter(
				pessoa=self.pessoa,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
			).exists()
		)

	def test_envio_manual_nao_cria_email(self):
		# E-mail desativado no envio manual: canal=email nao gera mensagem de e-mail.
		url = reverse('pessoas-enfileirar-mensagem', args=[self.pessoa.pk])
		resp = self.client.post(
			url,
			{
				'canal': MensagemContato.CanalChoices.EMAIL,
				'conteudo': 'Oi por email.',
			},
		)

		self.assertEqual(resp.status_code, 302)
		self.assertFalse(
			MensagemContato.objects.filter(
				pessoa=self.pessoa,
				canal=MensagemContato.CanalChoices.EMAIL,
			).exists()
		)

	def test_envio_manual_fixa_whatsapp(self):
		# Mesmo enviando canal=email, o form ignora e usa WhatsApp (canal fixo).
		self.pessoa.iniciou_interacao = True
		self.pessoa.save(update_fields=['iniciou_interacao'])
		# Entrada recente -> janela de 24h aberta (senao o envio livre e bloqueado).
		MensagemContato.objects.create(
			pessoa=self.pessoa,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo='Oi',
		)
		url = reverse('pessoas-enfileirar-mensagem', args=[self.pessoa.pk])
		resp = self.client.post(
			url,
			{
				'canal': MensagemContato.CanalChoices.EMAIL,
				'conteudo': 'Mensagem manual.',
			},
		)

		self.assertEqual(resp.status_code, 302)
		self.assertFalse(
			MensagemContato.objects.filter(pessoa=self.pessoa, canal=MensagemContato.CanalChoices.EMAIL).exists()
		)
		self.assertTrue(
			MensagemContato.objects.filter(
				pessoa=self.pessoa,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
			).exists()
		)

	def test_processador_cancela_whatsapp_sem_resposta_ao_template(self):
		mensagem = MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Mensagem normal antes do aceite.',
		)

		resultado = processar_fila_mensagens(limit=5)
		mensagem.refresh_from_db()

		self.assertEqual(resultado['total_processado'], 1)
		self.assertEqual(resultado['falha'], 1)
		self.assertEqual(mensagem.status_fila, MensagemContato.StatusFilaChoices.CANCELADA)
		self.assertIn('ainda nao respondeu', mensagem.erro_ultimo_envio)
		self.assertEqual(mensagem.metadata_envio['bloqueio_envio']['motivo'], 'whatsapp_sem_interacao_previa')

	def test_processador_permite_template_primeiro_contato_sem_resposta(self):
		mensagem = MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Template opt-in enfileirado',
			metadata_envio={'tipo_template': 'primeiro_contato_opt_in'},
		)

		resultado = processar_fila_mensagens(limit=5, dry_run=True)
		mensagem.refresh_from_db()

		self.assertEqual(resultado['total_processado'], 1)
		self.assertEqual(resultado['falha'], 0)
		self.assertEqual(mensagem.status_fila, MensagemContato.StatusFilaChoices.PENDENTE)

	def test_webhook_resposta_libera_envio_whatsapp_para_pessoa_existente(self):
		resp = self.client.post(
			reverse('mensagens-webhook-twilio-inbound'),
			{
				'From': 'whatsapp:+5531955554444',
				'MessageSid': 'SM123',
				'Body': 'Pode me mandar mensagem.',
			},
		)

		self.assertEqual(resp.status_code, 200)
		self.pessoa.refresh_from_db()
		self.assertTrue(self.pessoa.iniciou_interacao)
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		self.assertTrue(
			self.pessoa.interacoes.filter(
				tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
				descricao__icontains='liberou',
			).exists()
		)


class BoasVindasEvolutionTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user('equipe_evolution', password='x', is_staff=True)
		self.client.force_login(self.user)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Visitante',
			telefone_whatsapp='31955554444',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
		)

	@override_settings(WHATSAPP_PROVIDER='evolution', TWILIO_TEMPLATE_OPT_IN_SID='', EVOLUTION_TEXTO_OPTIN='')
	def test_dashboard_enfileira_boas_vindas_evolution_sem_sid(self):
		TemplateWhatsapp.objects.create(
			tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO,
			texto_evolution='Bem-vindo, {nome}!',
		)

		resp = self.client.post(reverse('dashboard'), {'action': 'disparar_template_opt_in'})
		self.assertEqual(resp.status_code, 302)

		mensagem = MensagemContato.objects.get(pessoa=self.pessoa)
		self.assertEqual(mensagem.conteudo, 'Bem-vindo, Visitante!')
		self.assertEqual(mensagem.metadata_envio['tipo_template'], 'primeiro_contato_opt_in')
		self.assertEqual(mensagem.metadata_envio['evolution_texto'], 'Bem-vindo, Visitante!')
		self.assertNotIn('twilio_template', mensagem.metadata_envio)

		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.status, PrimeiroContato.StatusAcolhimento.ROBO)

	@override_settings(WHATSAPP_PROVIDER='evolution', TWILIO_TEMPLATE_OPT_IN_SID='', EVOLUTION_TEXTO_OPTIN='')
	def test_dashboard_exige_texto_evolution(self):
		resp = self.client.post(reverse('dashboard'), {'action': 'disparar_template_opt_in'})
		self.assertEqual(resp.status_code, 302)
		self.assertFalse(MensagemContato.objects.filter(pessoa=self.pessoa).exists())


class PermissaoConversasPessoasTests(TestCase):
	def setUp(self):
		self.factory = RequestFactory()
		self.user = get_user_model().objects.create_user(username='mensagens', password='x', is_staff=False)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Visitante',
			telefone_whatsapp='31966666666',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INDICACAO,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.FAZER_AMIZADES,
		)

	def test_usuario_com_permissao_acessa_conversa_individual(self):
		permission = Permission.objects.get(codename='pode_conversar_pessoas')
		self.user.user_permissions.add(permission)
		request = self.factory.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		request.user = self.user

		resp = PrimeiroContatoMensagensView.as_view()(request, pk=self.pessoa.pk)

		self.assertEqual(resp.status_code, 200)
		self.assertTrue(self.user.has_perm(PERMISSAO_CONVERSAR_PESSOAS))

	def test_usuario_com_permissao_nao_acessa_telas_administrativas_de_mensagem(self):
		permission = Permission.objects.get(codename='pode_conversar_pessoas')
		self.user.user_permissions.add(permission)
		views_bloqueadas = [
			(reverse('mensagens-fila'), MensagemFilaListView.as_view()),
			(reverse('mensagens-processamento'), ProcessamentoFilaControleView.as_view()),
			(reverse('mensagens-disparo-massa'), DisparoMensagemMassaView.as_view()),
			(reverse('usuarios-lista'), UsuarioListView.as_view()),
		]

		for url, view in views_bloqueadas:
			with self.subTest(url=url):
				request = self.factory.get(url)
				request.user = self.user
				with self.assertRaises(PermissionDenied):
					view(request)

	def test_staff_nao_acessa_telas_da_engrenagem(self):
		staff = get_user_model().objects.create_user(username='staff', password='x', is_staff=True)
		request = self.factory.get(reverse('mensagens-processamento'))
		request.user = staff

		with self.assertRaises(PermissionDenied):
			ProcessamentoFilaControleView.as_view()(request)

		request = self.factory.get(reverse('usuarios-lista'))
		request.user = staff

		with self.assertRaises(PermissionDenied):
			UsuarioListView.as_view()(request)

	def test_usuario_sem_permissao_nao_acessa_conversa_individual(self):
		request = self.factory.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		request.user = self.user

		with self.assertRaises(PermissionDenied):
			PrimeiroContatoMensagensView.as_view()(request, pk=self.pessoa.pk)


class RelatorioPessoasTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user(username='equipe', password='x', is_staff=True)
		for nome, telefone, status in [
			('Ana', '31999990001', PrimeiroContato.StatusAcolhimento.PARTICIPANTE),
			('Bruno', '31999990002', PrimeiroContato.StatusAcolhimento.MEMBRO),
			('Carla', '31999990003', PrimeiroContato.StatusAcolhimento.PARTICIPANTE),
		]:
			PrimeiroContato.objects.create(
				nome=nome,
				telefone_whatsapp=telefone,
				primeira_vez=True,
				como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
				o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
				status=status,
			)
		self.url = reverse('pessoas-relatorios')
		self.client.force_login(self.user)

	def _params(self, **extra):
		dados = {'acao': 'gerar', 'formato': 'csv', 'colunas': ['nome', 'status']}
		dados.update(extra)
		return dados

	def test_login_obrigatorio(self):
		self.client.logout()
		resp = self.client.get(self.url)
		self.assertEqual(resp.status_code, 302)
		self.assertIn('/login/', resp['Location'])

	def test_gera_csv_com_colunas_escolhidas(self):
		resp = self.client.get(self.url, self._params(formato='csv'))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp['Content-Type'], 'text/csv; charset=utf-8')
		self.assertIn('attachment; filename="relatorio_pessoas_', resp['Content-Disposition'])
		conteudo = resp.content.decode('utf-8')
		self.assertIn('Nome', conteudo)
		self.assertIn('Status', conteudo)
		self.assertNotIn('WhatsApp', conteudo)

	def test_gera_xlsx(self):
		resp = self.client.get(self.url, self._params(formato='xlsx'))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(
			resp['Content-Type'],
			'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
		)

	def test_gera_pdf(self):
		resp = self.client.get(self.url, self._params(formato='pdf'))
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(resp['Content-Type'], 'application/pdf')
		self.assertTrue(resp.content.startswith(b'%PDF'))

	def test_filtro_status_reduz_registros(self):
		resp = self.client.get(self.url, self._params(formato='csv', status='participante'))
		linhas = [linha for linha in resp.content.decode('utf-8').splitlines() if linha.strip()]
		self.assertEqual(len(linhas), 3)  # cabecalho + 2 participantes

	def test_form_sem_colunas_invalido(self):
		form = RelatorioPessoasForm(data={'formato': 'csv'})
		self.assertFalse(form.is_valid())
		self.assertIn('colunas', form.errors)
		self.assertIn('ao menos uma coluna', form.errors['colunas'][0])

	def test_colunas_seguem_ordem_canonica(self):
		form = RelatorioPessoasForm(data={'formato': 'csv', 'colunas': ['status', 'nome']})
		self.assertTrue(form.is_valid(), form.errors)
		self.assertEqual(form.cleaned_data['colunas'], ['nome', 'status'])

	def test_filtro_por_responsavel(self):
		from apps.acolhimento.reports import filtrar_pessoas

		voluntario = get_user_model().objects.create_user(username='voluntario', password='x')
		ana = PrimeiroContato.objects.get(nome='Ana')
		ana.responsavel_atual = voluntario
		ana.save(update_fields=['responsavel_atual'])

		com_resp = filtrar_pessoas({'responsavel': str(voluntario.id)})
		self.assertEqual(list(com_resp.values_list('nome', flat=True)), ['Ana'])

		sem_resp = filtrar_pessoas({'responsavel': 'sem'})
		self.assertEqual(sorted(sem_resp.values_list('nome', flat=True)), ['Bruno', 'Carla'])

	def test_resumo_por_status(self):
		from apps.acolhimento.reports import resumo_pessoas

		resumo = resumo_pessoas(PrimeiroContato.objects.all())
		self.assertEqual(resumo['total'], 3)
		por_status = {item['rotulo']: item['total'] for item in resumo['por_status']}
		self.assertEqual(por_status.get('Participante'), 2)
		self.assertEqual(por_status.get('Membro'), 1)

	def test_usuario_comum_nao_emite_relatorio(self):
		comum = get_user_model().objects.create_user(username='comum', password='x')
		request = RequestFactory().get(self.url)
		request.user = comum
		with self.assertRaises(PermissionDenied):
			RelatorioPessoasView.as_view()(request)

	def test_admin_emite_relatorio(self):
		# o usuario do setUp e is_staff -> deve conseguir gerar
		resp = self.client.get(self.url, self._params(formato='csv'))
		self.assertEqual(resp.status_code, 200)


class QuestionarioTests(TestCase):
	def setUp(self):
		self.admin = get_user_model().objects.create_user(
			'admin', password='x', is_staff=True, is_superuser=True,
		)
		self.staff_admin = get_user_model().objects.create_user('staff_admin', password='x', is_staff=True)
		self.comum = get_user_model().objects.create_user('comum', password='x')
		self.pessoa = PrimeiroContato.objects.create(
			nome='Ana',
			telefone_whatsapp='31999990001',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.PARTICIPANTE,
		)
		self.questionario = Questionario.objects.create(titulo='Q1', ativo=True)
		self.p_texto = PerguntaQuestionario.objects.create(
			questionario=self.questionario, texto='Seu nome?', tipo='texto_curto', obrigatoria=True, ordem=0,
		)
		self.p_escolha = PerguntaQuestionario.objects.create(
			questionario=self.questionario, texto='Sexo?', tipo='escolha_unica', obrigatoria=True, ordem=1,
		)
		self.op_m = OpcaoPergunta.objects.create(pergunta=self.p_escolha, texto='Masculino', ordem=0)
		self.op_f = OpcaoPergunta.objects.create(pergunta=self.p_escolha, texto='Feminino', ordem=1)

	def test_gerar_convite_cria_token_pendente(self):
		self.client.force_login(self.admin)
		resp = self.client.post(
			reverse('pessoas-gerar-convite', args=[self.pessoa.pk]),
			{'questionario': self.questionario.pk},
		)
		self.assertEqual(resp.status_code, 302)
		convite = ConviteQuestionario.objects.get(pessoa=self.pessoa)
		self.assertEqual(convite.status, ConviteQuestionario.StatusConvite.PENDENTE)
		self.assertIsNotNone(convite.token)
		self.assertTrue(
			self.pessoa.interacoes.filter(
				tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
				descricao__icontains='gerado para preenchimento',
			).exists()
		)

	def test_responder_publico_sem_login_salva(self):
		convite = ConviteQuestionario.objects.create(questionario=self.questionario, pessoa=self.pessoa)
		data = {
			'respondente_nome': 'Ana',
			f'pergunta_{self.p_texto.id}': 'Ana Maria',
			f'pergunta_{self.p_escolha.id}': str(self.op_f.id),
		}
		# cliente anonimo (sem login) posta no link publico
		resp = self.client.post(reverse('responder-questionario', args=[convite.token]), data)
		self.assertRedirects(resp, reverse('responder-questionario-sucesso'), fetch_redirect_response=False)
		convite.refresh_from_db()
		self.assertTrue(convite.respondido)
		self.assertEqual(convite.respondente_nome, 'Ana')
		self.assertEqual(convite.respostas.count(), 2)
		self.assertEqual(convite.respostas.get(pergunta=self.p_escolha).opcao_id, self.op_f.id)
		self.assertTrue(
			self.pessoa.interacoes.filter(
				tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
				descricao__icontains='respondido por Ana',
			).exists()
		)

	def test_responder_obrigatoria_faltando_invalida(self):
		form = ResponderQuestionarioForm(
			data={'respondente_nome': 'X', f'pergunta_{self.p_escolha.id}': str(self.op_f.id)},
			questionario=self.questionario,
		)
		self.assertFalse(form.is_valid())
		self.assertIn(f'pergunta_{self.p_texto.id}', form.errors)

	def test_pergunta_form_parseia_opcoes(self):
		form = PerguntaForm(data={
			'texto': 'Cor favorita?',
			'tipo': 'escolha_unica',
			'opcoes_texto': 'Azul\nVerde\n\nVermelho',
		})
		self.assertTrue(form.is_valid(), form.errors)
		pergunta = form.save(commit=False)
		pergunta.questionario = self.questionario
		pergunta.save()
		form.salvar_opcoes(pergunta)
		self.assertEqual(list(pergunta.opcoes.values_list('texto', flat=True)), ['Azul', 'Verde', 'Vermelho'])

	def test_pergunta_escolha_sem_opcoes_invalida(self):
		form = PerguntaForm(data={'texto': 'X?', 'tipo': 'escolha_unica', 'opcoes_texto': ''})
		self.assertFalse(form.is_valid())
		self.assertIn('opcoes_texto', form.errors)

	def test_construtor_e_admin_only(self):
		request = RequestFactory().get(reverse('questionarios-lista'))
		request.user = self.comum
		with self.assertRaises(PermissionDenied):
			QuestionarioListView.as_view()(request)

		request = RequestFactory().get(reverse('questionarios-lista'))
		request.user = self.staff_admin
		with self.assertRaises(PermissionDenied):
			QuestionarioListView.as_view()(request)

		request = RequestFactory().get(reverse('questionarios-lista'))
		request.user = self.admin
		resp = QuestionarioListView.as_view()(request)
		self.assertEqual(resp.status_code, 200)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_menu_configuracoes_staff_ve_apenas_qrcodes(self):
		# Staff enxerga a engrenagem, mas apenas com QR Codes: os itens de
		# superuser (Usuarios, Processar fila, Questionarios) continuam ocultos.
		self.client.force_login(self.staff_admin)
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'settings-navigation')
		self.assertContains(resp, 'QR Codes')
		self.assertNotContains(resp, 'Usuarios')
		self.assertNotContains(resp, 'Processar fila')
		self.assertNotContains(resp, 'Questionarios')

		# Superuser continua vendo tudo, inclusive QR Codes.
		self.client.force_login(self.admin)
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'settings-navigation')
		self.assertContains(resp, 'QR Codes')
		self.assertContains(resp, 'Usuarios')
		self.assertContains(resp, 'Processar fila')
		self.assertContains(resp, 'Questionarios')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_staff_gera_convite_questionario(self):
		self.client.force_login(self.staff_admin)
		resp = self.client.post(
			reverse('pessoas-gerar-convite', args=[self.pessoa.pk]),
			{'questionario': self.questionario.pk},
		)
		self.assertEqual(resp.status_code, 302)
		self.assertEqual(ConviteQuestionario.objects.filter(pessoa=self.pessoa).count(), 1)

	def test_permissao_mensagens_gera_mas_nao_exclui_convite(self):
		# Usuario com a permissao 'pode_conversar_pessoas' (nao staff/super):
		# pode gerar o link, mas nao pode excluir.
		operador = get_user_model().objects.create_user('operador', password='x')
		perm = Permission.objects.get(codename='pode_conversar_pessoas', content_type__app_label='acolhimento')
		operador.user_permissions.add(perm)
		self.client.force_login(operador)

		# Pode gerar (302, redirect sem render)
		resp = self.client.post(
			reverse('pessoas-gerar-convite', args=[self.pessoa.pk]),
			{'questionario': self.questionario.pk},
		)
		self.assertEqual(resp.status_code, 302)
		convite = ConviteQuestionario.objects.get(pessoa=self.pessoa)

		# NAO pode excluir -> PermissionDenied
		request = RequestFactory().post(reverse('convite-excluir', args=[convite.pk]))
		request.user = operador
		with self.assertRaises(PermissionDenied):
			ExcluirConviteQuestionarioView.as_view()(request, pk=convite.pk)
		self.assertTrue(ConviteQuestionario.objects.filter(pk=convite.pk).exists())

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_novo_questionario_renderiza_form_para_admin(self):
		self.client.force_login(self.admin)
		resp = self.client.get(reverse('questionarios-novo'))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Dados do questionario')
		self.assertContains(resp, 'Questionario ativo')
		self.assertContains(resp, 'Salvar questionario')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_builder_questionario_renderiza_para_admin(self):
		self.client.force_login(self.admin)
		resp = self.client.get(reverse('questionario-builder', args=[self.questionario.pk]))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Estrutura')
		self.assertContains(resp, 'Adicionar pergunta')
		self.assertContains(resp, 'Seu nome?')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_editar_pergunta_renderiza_form_para_admin(self):
		self.client.force_login(self.admin)
		resp = self.client.get(reverse('pergunta-editar', args=[self.p_escolha.pk]))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Dados da pergunta')
		self.assertContains(resp, 'Salvar pergunta')
		self.assertContains(resp, 'Feminino')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_visualizar_questionario_renderiza_previa_para_admin(self):
		self.client.force_login(self.admin)
		resp = self.client.get(reverse('questionario-visualizar', args=[self.questionario.pk]))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Visualizar questionario')
		self.assertContains(resp, 'Seu nome?')
		self.assertContains(resp, 'Feminino')

	def test_gerar_convite_nao_duplica_no_clique_duplo(self):
		self.client.force_login(self.admin)
		url = reverse('pessoas-gerar-convite', args=[self.pessoa.pk])
		self.client.post(url, {'questionario': self.questionario.pk})
		self.client.post(url, {'questionario': self.questionario.pk})
		self.assertEqual(
			ConviteQuestionario.objects.filter(pessoa=self.pessoa, questionario=self.questionario).count(),
			1,
		)

	def test_gerar_convite_bloqueado_fora_de_participante(self):
		self.pessoa.status = PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
		self.pessoa.save(update_fields=['status'])
		self.client.force_login(self.admin)
		self.client.post(
			reverse('pessoas-gerar-convite', args=[self.pessoa.pk]),
			{'questionario': self.questionario.pk},
		)
		self.assertFalse(ConviteQuestionario.objects.filter(pessoa=self.pessoa).exists())

	def test_excluir_convite(self):
		convite = ConviteQuestionario.objects.create(questionario=self.questionario, pessoa=self.pessoa)
		self.client.force_login(self.admin)
		resp = self.client.post(reverse('convite-excluir', args=[convite.pk]))
		self.assertEqual(resp.status_code, 302)
		self.assertFalse(ConviteQuestionario.objects.filter(pk=convite.pk).exists())
		self.assertTrue(
			self.pessoa.interacoes.filter(
				tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
				descricao__icontains='apagado',
			).exists()
		)

	def test_enviar_convite_whatsapp_registra_timeline(self):
		self.pessoa.iniciou_interacao = True
		self.pessoa.save(update_fields=['iniciou_interacao'])
		# Entrada recente -> janela de 24h aberta.
		MensagemContato.objects.create(
			pessoa=self.pessoa,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo='Oi',
		)
		convite = ConviteQuestionario.objects.create(questionario=self.questionario, pessoa=self.pessoa)
		self.client.force_login(self.admin)
		resp = self.client.post(reverse('convite-enviar-whatsapp', args=[convite.pk]))
		self.assertEqual(resp.status_code, 302)
		self.assertTrue(
			MensagemContato.objects.filter(
				pessoa=self.pessoa,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				conteudo__icontains=self.questionario.titulo,
			).exists()
		)
		self.assertTrue(
			self.pessoa.interacoes.filter(
				tipo=InteracaoAcolhimento.TipoInteracao.TENTATIVA_CONTATO,
				descricao__icontains='WhatsApp',
			).exists()
		)


class JanelaWhatsappTests(TestCase):
	def setUp(self):
		self.user = get_user_model().objects.create_user('equipe_janela', password='x', is_staff=True)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Zeca',
			telefone_whatsapp='31988887777',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			iniciou_interacao=True,
		)
		self.client.force_login(self.user)

	def _entrada(self, quando):
		msg = MensagemContato.objects.create(
			pessoa=self.pessoa,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo='oi',
		)
		MensagemContato.objects.filter(pk=msg.pk).update(enfileirada_em=quando)
		return msg

	def test_janela_aberta_com_entrada_recente(self):
		from apps.acolhimento.whatsapp_rules import janela_atendimento_aberta
		self._entrada(timezone.now() - timedelta(hours=1))
		self.assertTrue(janela_atendimento_aberta(self.pessoa))

	def test_janela_fechada_sem_entrada_recente(self):
		from apps.acolhimento.whatsapp_rules import janela_atendimento_aberta
		self._entrada(timezone.now() - timedelta(hours=30))
		self.assertFalse(janela_atendimento_aberta(self.pessoa))

	def test_processador_cancela_livre_com_janela_fechada(self):
		self._entrada(timezone.now() - timedelta(hours=30))
		mensagem = MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Mensagem livre fora da janela.',
		)
		resultado = processar_fila_mensagens(limit=5)
		mensagem.refresh_from_db()
		self.assertEqual(resultado['falha'], 1)
		self.assertEqual(mensagem.status_fila, MensagemContato.StatusFilaChoices.CANCELADA)
		self.assertEqual(mensagem.metadata_envio['bloqueio_envio']['motivo'], 'whatsapp_janela_24h_fechada')

	def test_template_continuar_isento_da_janela(self):
		# Janela fechada, mas o template de continuacao nao e bloqueado.
		mensagem = MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Template de continuacao',
			metadata_envio={'tipo_template': 'continuar_conversa'},
		)
		resultado = processar_fila_mensagens(limit=5, dry_run=True)
		mensagem.refresh_from_db()
		self.assertEqual(resultado['total_processado'], 1)
		self.assertEqual(resultado['falha'], 0)

	def test_enfileirar_livre_bloqueado_com_janela_fechada(self):
		self._entrada(timezone.now() - timedelta(hours=30))
		resp = self.client.post(
			reverse('pessoas-enfileirar-mensagem', args=[self.pessoa.pk]),
			{'conteudo': 'Oi, tudo bem?'},
		)
		self.assertEqual(resp.status_code, 302)
		self.assertFalse(
			MensagemContato.objects.filter(
				pessoa=self.pessoa,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
			).exists()
		)

	@override_settings(TWILIO_TEMPLATE_CONTINUAR_SID='HXtestecontinuar')
	def test_enviar_template_continuar_enfileira(self):
		resp = self.client.post(reverse('pessoas-template-continuar', args=[self.pessoa.pk]))
		self.assertEqual(resp.status_code, 302)
		mensagem = MensagemContato.objects.filter(
			pessoa=self.pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA
		).first()
		self.assertIsNotNone(mensagem)
		self.assertEqual(mensagem.metadata_envio.get('tipo_template'), 'continuar_conversa')

	@override_settings(WHATSAPP_PROVIDER='evolution', TWILIO_TEMPLATE_CONTINUAR_SID='', EVOLUTION_TEXTO_CONTINUAR='')
	def test_enviar_continuar_evolution_sem_sid_usa_texto_configurado(self):
		TemplateWhatsapp.objects.create(
			tipo=TemplateWhatsapp.Tipo.CONTINUAR,
			texto_evolution='Oi {nome}, podemos continuar?',
		)

		resp = self.client.post(reverse('pessoas-template-continuar', args=[self.pessoa.pk]))
		self.assertEqual(resp.status_code, 302)
		mensagem = MensagemContato.objects.filter(
			pessoa=self.pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA
		).first()

		self.assertIsNotNone(mensagem)
		self.assertEqual(mensagem.conteudo, 'Oi Zeca, podemos continuar?')
		self.assertEqual(mensagem.metadata_envio.get('tipo_template'), 'continuar_conversa')
		self.assertEqual(mensagem.metadata_envio.get('evolution_texto'), 'Oi Zeca, podemos continuar?')
		self.assertNotIn('twilio_template', mensagem.metadata_envio)

	def _templates_continuar(self):
		return MensagemContato.objects.filter(
			pessoa=self.pessoa,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			metadata_envio__tipo_template='continuar_conversa',
		)

	@override_settings(TWILIO_TEMPLATE_CONTINUAR_SID='HXtestecontinuar')
	def test_template_continuar_bloqueia_reenvio_em_24h(self):
		from apps.acolhimento.whatsapp_rules import pode_enviar_template_continuar
		url = reverse('pessoas-template-continuar', args=[self.pessoa.pk])
		self.client.post(url)
		self.assertEqual(self._templates_continuar().count(), 1)
		self.assertFalse(pode_enviar_template_continuar(self.pessoa))
		# Reenvio dentro das 24h e bloqueado (evita cobranca duplicada).
		resp = self.client.post(url)
		self.assertEqual(resp.status_code, 302)
		self.assertEqual(self._templates_continuar().count(), 1)

	@override_settings(TWILIO_TEMPLATE_CONTINUAR_SID='HXtestecontinuar')
	def test_template_continuar_liberado_apos_24h(self):
		from apps.acolhimento.whatsapp_rules import pode_enviar_template_continuar
		url = reverse('pessoas-template-continuar', args=[self.pessoa.pk])
		self.client.post(url)
		self._templates_continuar().update(enfileirada_em=timezone.now() - timedelta(hours=25))
		self.assertTrue(pode_enviar_template_continuar(self.pessoa))
		resp = self.client.post(url)
		self.assertEqual(resp.status_code, 302)
		self.assertEqual(self._templates_continuar().count(), 2)

	def test_template_continuar_falha_nao_bloqueia_reenvio(self):
		from apps.acolhimento.whatsapp_rules import pode_enviar_template_continuar
		MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.FALHA,
			conteudo='Template de continuacao',
			metadata_envio={'tipo_template': 'continuar_conversa'},
		)
		self.assertTrue(pode_enviar_template_continuar(self.pessoa))

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES, TWILIO_TEMPLATE_CONTINUAR_SID='HXtestecontinuar')
	def test_tela_mostra_botao_quando_pode_enviar_template(self):
		resp = self.client.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Enviar template de continuacao')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES, TWILIO_TEMPLATE_CONTINUAR_SID='HXtestecontinuar')
	def test_tela_mostra_espera_apos_enviar_template(self):
		MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Template de continuacao',
			metadata_envio={'tipo_template': 'continuar_conversa'},
		)
		resp = self.client.get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'Reenvio liberado em')
		self.assertNotContains(resp, 'Enviar template de continuacao')


class PermissaoConversaTests(TestCase):
	"""Usuarios com 'pode_conversar_pessoas' (nao staff/super) usam a tela de conversa."""

	def setUp(self):
		self.operador = get_user_model().objects.create_user('operador_conv', password='x')
		perm = Permission.objects.get(codename='pode_conversar_pessoas', content_type__app_label='acolhimento')
		self.operador.user_permissions.add(perm)
		self.sem_perm = get_user_model().objects.create_user('sem_perm_conv', password='x')
		self.pessoa = PrimeiroContato.objects.create(
			nome='Contato',
			telefone_whatsapp='31955551111',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			iniciou_interacao=True,
		)
		# entrada recente -> janela de 24h aberta (permite envio livre)
		MensagemContato.objects.create(
			pessoa=self.pessoa,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo='oi',
		)

	def test_operador_enfileira_mensagem(self):
		self.client.force_login(self.operador)
		resp = self.client.post(
			reverse('pessoas-enfileirar-mensagem', args=[self.pessoa.pk]),
			{'conteudo': 'Ola do operador'},
		)
		self.assertEqual(resp.status_code, 302)
		self.assertTrue(
			MensagemContato.objects.filter(
				pessoa=self.pessoa,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
				conteudo='Ola do operador',
			).exists()
		)

	@override_settings(TWILIO_TEMPLATE_CONTINUAR_SID='HXtestecontinuar')
	def test_operador_envia_template_continuar(self):
		self.client.force_login(self.operador)
		resp = self.client.post(reverse('pessoas-template-continuar', args=[self.pessoa.pk]))
		self.assertEqual(resp.status_code, 302)
		mensagem = MensagemContato.objects.filter(
			pessoa=self.pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA
		).order_by('-id').first()
		self.assertEqual(mensagem.metadata_envio.get('tipo_template'), 'continuar_conversa')

	def test_sem_permissao_nao_acessa_conversa(self):
		request = RequestFactory().get(reverse('pessoas-mensagens', args=[self.pessoa.pk]))
		request.user = self.sem_perm
		with self.assertRaises(PermissionDenied):
			PrimeiroContatoMensagensView.as_view()(request, pk=self.pessoa.pk)


class DisparoMassaTests(TestCase):
	"""Disparo em massa em dois modos: mensagem livre (janela aberta) e template (marketing)."""

	def setUp(self):
		self.user = get_user_model().objects.create_user('equipe_massa', password='x', is_staff=True)
		self.client.force_login(self.user)
		self.pessoa_aberta = PrimeiroContato.objects.create(
			nome='Ana Aberta',
			telefone_whatsapp='31900001111',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.PARTICIPANTE,
			iniciou_interacao=True,
		)
		MensagemContato.objects.create(
			pessoa=self.pessoa_aberta,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo='oi',
		)
		# Sem entrada recente -> janela de 24h fechada.
		self.pessoa_fechada = PrimeiroContato.objects.create(
			nome='Bruno Fechado',
			telefone_whatsapp='31900002222',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.PARTICIPANTE,
			iniciou_interacao=True,
		)

	def _saidas(self, pessoa):
		return MensagemContato.objects.filter(
			pessoa=pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA
		)

	def test_modo_livre_so_envia_para_janela_aberta(self):
		resp = self.client.post(
			reverse('mensagens-disparo-massa'),
			{
				'modo': 'livre',
				'conteudo': 'Ola pessoal',
				'pessoas': [self.pessoa_aberta.pk, self.pessoa_fechada.pk],
			},
		)
		self.assertEqual(resp.status_code, 302)
		self.assertEqual(self._saidas(self.pessoa_aberta).count(), 1)
		self.assertEqual(self._saidas(self.pessoa_fechada).count(), 0)

	def test_modo_template_envia_para_todos_mesmo_com_janela_fechada(self):
		resp = self.client.post(
			reverse('mensagens-disparo-massa'),
			{
				'modo': 'template',
				'content_sid': 'HXabc123',
				'pessoas': [self.pessoa_aberta.pk, self.pessoa_fechada.pk],
			},
		)
		self.assertEqual(resp.status_code, 302)
		for pessoa in (self.pessoa_aberta, self.pessoa_fechada):
			mensagem = self._saidas(pessoa).first()
			self.assertIsNotNone(mensagem)
			self.assertEqual(mensagem.metadata_envio['tipo_template'], 'marketing_massa')
			self.assertEqual(mensagem.metadata_envio['twilio_template']['content_sid'], 'HXabc123')

	def test_modo_template_personaliza_nome_por_pessoa(self):
		resp = self.client.post(
			reverse('mensagens-disparo-massa'),
			{
				'modo': 'template',
				'content_sid': 'HXabc123',
				'content_variables': '{"1": "{nome}"}',
				'pessoas': [self.pessoa_aberta.pk],
			},
		)
		self.assertEqual(resp.status_code, 302)
		mensagem = self._saidas(self.pessoa_aberta).first()
		self.assertIn('Ana Aberta', mensagem.metadata_envio['twilio_template']['content_variables'])

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_modo_template_exige_content_sid(self):
		resp = self.client.post(
			reverse('mensagens-disparo-massa'),
			{
				'modo': 'template',
				'content_sid': '',
				'pessoas': [self.pessoa_aberta.pk],
			},
		)
		self.assertEqual(resp.status_code, 200)  # re-renderiza com erro de validacao
		self.assertEqual(
			MensagemContato.objects.filter(direcao=MensagemContato.DirecaoChoices.SAIDA).count(),
			0,
		)

	def test_template_marketing_isento_da_janela_no_processador(self):
		mensagem = MensagemContato.objects.create(
			pessoa=self.pessoa_fechada,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='[Template] HXabc123',
			metadata_envio={
				'tipo_template': 'marketing_massa',
				'twilio_template': {'content_sid': 'HXabc123', 'content_variables': ''},
			},
		)
		resultado = processar_fila_mensagens(limit=5, dry_run=True)
		mensagem.refresh_from_db()
		self.assertEqual(resultado['falha'], 0)
		self.assertEqual(resultado['total_processado'], 1)


class JanelaAbertaFiltroTests(TestCase):
	"""Filtro de janela de 24h na lista de pessoas + contagem no dashboard."""

	def setUp(self):
		self.user = get_user_model().objects.create_user('equipe_janela_lista', password='x', is_staff=True)
		self.client.force_login(self.user)
		self.aberta = PrimeiroContato.objects.create(
			nome='Com Janela',
			telefone_whatsapp='31900007777',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			iniciou_interacao=True,
		)
		# Entrada recente -> janela aberta.
		MensagemContato.objects.create(
			pessoa=self.aberta,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo='oi',
		)
		self.fechada = PrimeiroContato.objects.create(
			nome='Sem Janela',
			telefone_whatsapp='31900008888',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			iniciou_interacao=True,
		)
		# Entrada antiga (>24h) -> janela fechada.
		msg = MensagemContato.objects.create(
			pessoa=self.fechada,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.ENTRADA,
			status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
			conteudo='oi',
		)
		MensagemContato.objects.filter(pk=msg.pk).update(
			enfileirada_em=timezone.now() - timedelta(hours=30)
		)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_filtro_janela_aberta(self):
		resp = self.client.get(reverse('pessoas-lista'), {'janela': 'aberta'})
		nomes = [p.nome for p in resp.context['pessoas']]
		self.assertIn('Com Janela', nomes)
		self.assertNotIn('Sem Janela', nomes)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_filtro_janela_fechada(self):
		resp = self.client.get(reverse('pessoas-lista'), {'janela': 'fechada'})
		nomes = [p.nome for p in resp.context['pessoas']]
		self.assertIn('Sem Janela', nomes)
		self.assertNotIn('Com Janela', nomes)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_lista_sem_filtro_mostra_todas(self):
		resp = self.client.get(reverse('pessoas-lista'))
		nomes = [p.nome for p in resp.context['pessoas']]
		self.assertIn('Com Janela', nomes)
		self.assertIn('Sem Janela', nomes)

	def test_contador_janela_aberta(self):
		from apps.acolhimento.whatsapp_rules import contar_pessoas_janela_aberta
		self.assertEqual(contar_pessoas_janela_aberta(), 1)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_dashboard_expoe_total_janela_aberta(self):
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.context['total_janela_aberta'], 1)


class ConfiguracaoTemplatesTests(TestCase):
	"""Tela de configuracao dos templates padrao da Twilio (somente superusuario)."""

	def setUp(self):
		self.super = get_user_model().objects.create_superuser('super_cfg', 'super_cfg@ex.com', 'x')
		self.staff = get_user_model().objects.create_user('staff_cfg', password='x', is_staff=True)
		self.url = reverse('configuracao-templates')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_superuser_acessa(self):
		self.client.force_login(self.super)
		resp = self.client.get(self.url)
		self.assertEqual(resp.status_code, 200)

	def test_staff_nao_superuser_bloqueado(self):
		request = RequestFactory().get(self.url)
		request.user = self.staff
		with self.assertRaises(PermissionDenied):
			ConfiguracaoTemplatesView.as_view()(request)

	def test_salva_sobrescreve_env(self):
		from apps.acolhimento import template_config
		self.client.force_login(self.super)
		resp = self.client.post(self.url, {
			'opt_in_sid': 'HXoptinNOVO',
			'opt_in_variables': '{"2": "abc"}',
			'opt_in_texto_evolution': 'Boas-vindas {nome}',
			'continuar_sid': 'HXcontinuarNOVO',
			'continuar_variables': '{}',
			'continuar_texto_evolution': 'Continuar {nome}',
		})
		self.assertEqual(resp.status_code, 302)
		self.assertEqual(TemplateWhatsapp.objects.count(), 2)
		self.assertEqual(template_config.opt_in_sid(), 'HXoptinNOVO')
		self.assertEqual(template_config.continuar_sid(), 'HXcontinuarNOVO')
		self.assertEqual(template_config.opt_in_texto_evolution(), 'Boas-vindas {nome}')
		self.assertEqual(template_config.continuar_texto_evolution(), 'Continuar {nome}')

	@override_settings(TWILIO_TEMPLATE_CONTINUAR_SID='HXvemdoenv')
	def test_fallback_env_quando_sem_config(self):
		from apps.acolhimento import template_config
		self.assertFalse(TemplateWhatsapp.objects.filter(tipo=TemplateWhatsapp.Tipo.CONTINUAR).exists())
		self.assertEqual(template_config.continuar_sid(), 'HXvemdoenv')

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_sid_invalido_rejeitado(self):
		self.client.force_login(self.super)
		resp = self.client.post(self.url, {
			'opt_in_sid': 'ZZinvalido',
			'opt_in_variables': '{}',
			'continuar_sid': '',
			'continuar_variables': '{}',
		})
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(TemplateWhatsapp.objects.count(), 0)

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_variables_json_invalido_rejeitado(self):
		self.client.force_login(self.super)
		resp = self.client.post(self.url, {
			'opt_in_sid': 'HXok',
			'opt_in_variables': 'nao-e-json',
			'continuar_sid': '',
			'continuar_variables': '{}',
		})
		self.assertEqual(resp.status_code, 200)
		self.assertEqual(TemplateWhatsapp.objects.count(), 0)


class EvolutionGatewayTextoTests(TestCase):
	@override_settings(WHATSAPP_PROVIDER='evolution', EVOLUTION_TEXTO_OPTIN='')
	def test_gateway_usa_texto_configurado_no_banco(self):
		from apps.acolhimento import whatsapp_gateway

		pessoa = PrimeiroContato.objects.create(
			nome='Lia',
			telefone_whatsapp='31999990000',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
		)
		TemplateWhatsapp.objects.create(
			tipo=TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO,
			texto_evolution='Ola {nome}, seja bem-vinda!',
		)
		mensagem = MensagemContato.objects.create(
			pessoa=pessoa,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Template opt-in enfileirado',
			metadata_envio={'tipo_template': 'primeiro_contato_opt_in'},
		)

		with patch('apps.acolhimento.evolution_service.send_whatsapp_text') as send:
			send.return_value = {'sid': 'EV123', 'status': 'sent'}
			envelope = whatsapp_gateway.enviar(mensagem, '+5531999990000')

		# Com a variacao anti-bloqueio a saudacao/emoji podem mudar, mas o corpo
		# configurado no banco (com o nome substituido) deve ser preservado, e o
		# delay de "digitando..." deve ser repassado.
		self.assertEqual(send.call_count, 1)
		_, kwargs = send.call_args
		self.assertEqual(kwargs['to_phone'], '+5531999990000')
		self.assertIn('Lia, seja bem-vinda!', kwargs['text'])
		self.assertNotIn('{nome}', kwargs['text'])
		self.assertGreater(kwargs['delay_ms'], 0)
		self.assertEqual(envelope['provider'], 'evolution')
		self.assertEqual(envelope['referencia_externa'], 'EV123')


class EvolutionWebhookConfigTests(TestCase):
	@override_settings(
		EVOLUTION_INSTANCE='pibvp-prod',
		EVOLUTION_WEBHOOK_URL='https://acolhimento.simoesti.com.br/acolhimento/mensagens/webhook/evolution/',
		EVOLUTION_WEBHOOK_EVENTS=['MESSAGES_UPSERT', 'MESSAGES_UPDATE'],
		EVOLUTION_WEBHOOK_BY_EVENTS=False,
		EVOLUTION_WEBHOOK_BASE64=False,
		EVOLUTION_WEBHOOK_SECRET='segredo-test',
	)
	def test_configure_webhook_envia_payload_da_instancia(self):
		from apps.acolhimento import evolution_service

		with patch('apps.acolhimento.evolution_service._post') as post:
			post.return_value = (201, {'success': True})
			data = evolution_service.configure_webhook()

		post.assert_called_once_with(
			'/webhook/set/pibvp-prod',
			{
				'enabled': True,
				'url': 'https://acolhimento.simoesti.com.br/acolhimento/mensagens/webhook/evolution/',
				'webhookByEvents': False,
				'webhookBase64': False,
				'events': ['MESSAGES_UPSERT', 'MESSAGES_UPDATE'],
				'headers': {'X-Evolution-Webhook-Secret': 'segredo-test'},
			},
		)
		self.assertEqual(data, {'success': True})

	@override_settings(
		EVOLUTION_INSTANCE='pibvp-prod',
		EVOLUTION_WEBHOOK_URL='https://acolhimento.simoesti.com.br/acolhimento/mensagens/webhook/evolution/',
		EVOLUTION_WEBHOOK_EVENTS=['MESSAGES_UPSERT'],
		EVOLUTION_WEBHOOK_BY_EVENTS=False,
		EVOLUTION_WEBHOOK_BASE64=False,
		EVOLUTION_WEBHOOK_SECRET='',
	)
	def test_configure_webhook_tenta_payload_envelopado_em_build_legada(self):
		from apps.acolhimento import evolution_service

		erro = evolution_service.EvolutionWhatsAppError(
			'API de WhatsApp HTTP 400: {\'response\': {\'message\': [[\'instance requires property "webhook"\']]}}'
		)
		payload = {
			'enabled': True,
			'url': 'https://acolhimento.simoesti.com.br/acolhimento/mensagens/webhook/evolution/',
			'webhookByEvents': False,
			'webhookBase64': False,
			'events': ['MESSAGES_UPSERT'],
		}

		with patch('apps.acolhimento.evolution_service._post') as post:
			post.side_effect = [erro, (201, {'success': True})]
			data = evolution_service.configure_webhook()

		self.assertEqual(post.call_args_list[0].args, ('/webhook/set/pibvp-prod', payload))
		self.assertEqual(post.call_args_list[1].args, ('/webhook/set/pibvp-prod', {'webhook': payload}))
		self.assertEqual(data, {'success': True})

	@override_settings(EVOLUTION_WEBHOOK_SECRET='segredo-test')
	def test_webhook_rejeita_header_invalido(self):
		resp = self.client.post(
			reverse('mensagens-webhook-evolution'),
			data='{}',
			content_type='application/json',
		)

		self.assertEqual(resp.status_code, 403)

	@override_settings(EVOLUTION_WEBHOOK_SECRET='segredo-test')
	def test_webhook_aceita_header_configurado(self):
		resp = self.client.post(
			reverse('mensagens-webhook-evolution'),
			data='{}',
			content_type='application/json',
			HTTP_X_EVOLUTION_WEBHOOK_SECRET='segredo-test',
		)

		self.assertEqual(resp.status_code, 200)


class AtendimentoBotTests(TestCase):
	"""Bot de menu do atendimento automatico (WhatsApp)."""

	def _pessoa(self, telefone='11970002222'):
		return PrimeiroContato.objects.create(
			nome='Visitante Bot',
			telefone_whatsapp=telefone,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.OUTRO,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.PARTICIPAR_DE_ALGO,
			origem_cadastro=PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO,
			status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
		)

	def _saidas(self, pessoa):
		return MensagemContato.objects.filter(
			pessoa=pessoa, direcao=MensagemContato.DirecaoChoices.SAIDA
		)

	def test_encontrar_opcao_por_numero_e_palavra(self):
		opcoes = atendimento_bot.opcoes_ativas()
		self.assertEqual(len(opcoes), 2)  # semeadas pela migration 0020
		self.assertEqual(atendimento_bot.encontrar_opcao('1', opcoes), opcoes[0])
		self.assertEqual(atendimento_bot.encontrar_opcao('quero o horario', opcoes), opcoes[1])
		self.assertIsNone(atendimento_bot.encontrar_opcao('xyzabc', opcoes))

	def test_bot_desligado_nao_responde(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = False
		cfg.save()
		pessoa = self._pessoa()
		self.assertFalse(atendimento_bot.processar_entrada(pessoa, 'oi'))
		self.assertEqual(self._saidas(pessoa).count(), 0)

	def test_bot_saudacao_na_primeira_mensagem(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.save()
		pessoa = self._pessoa()
		self.assertTrue(atendimento_bot.processar_entrada(pessoa, 'oi'))
		pessoa.refresh_from_db()
		self.assertEqual(self._saidas(pessoa).count(), 1)
		self.assertIn('Falar com alguem', self._saidas(pessoa).first().conteudo)
		self.assertEqual(pessoa.bot_etapa, PrimeiroContato.BotEtapaChoices.MENU)
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.ROBO)

	def test_bot_transfere_para_humano(self):
		cfg = ConfiguracaoAtendimentoBot.carregar()
		cfg.ativo = True
		cfg.save()
		pessoa = self._pessoa()
		atendimento_bot.processar_entrada(pessoa, 'oi')   # saudacao + menu
		pessoa.refresh_from_db()
		atendimento_bot.processar_entrada(pessoa, '1')    # opcao 1 = Falar com alguem (transferir)
		pessoa.refresh_from_db()
		self.assertEqual(pessoa.status, PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO)
		self.assertEqual(pessoa.bot_etapa, PrimeiroContato.BotEtapaChoices.INATIVO)
		self.assertFalse(atendimento_bot.processar_entrada(pessoa, 'oi de novo'))

	@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
	def test_pagina_config_superusuario_e_403(self):
		User = get_user_model()
		User.objects.create_user('bot_super', password='x', is_superuser=True, is_staff=True)
		User.objects.create_user('bot_comum', password='x')
		url = reverse('configuracao-atendimento')

		self.client.login(username='bot_super', password='x')
		self.assertEqual(self.client.get(url).status_code, 200)

		self.client.logout()
		self.client.login(username='bot_comum', password='x')
		self.assertEqual(self.client.get(url).status_code, 403)


@override_settings(STORAGES=SIMPLE_STATIC_STORAGES)
class ResponsavelAtualTests(TestCase):
	def setUp(self):
		User = get_user_model()
		self.staff = User.objects.create_user(username='staff_resp', password='x', is_staff=True, first_name='Ana')
		self.comum = User.objects.create_user(username='comum_resp', password='x', is_staff=False, first_name='Bia')
		self.pessoa = PrimeiroContato.objects.create(
			nome='Contato Um',
			telefone_whatsapp='31955550001',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
		)

	def test_staff_associa_responsavel(self):
		self.client.force_login(self.staff)
		resp = self.client.post(
			reverse('pessoas-responsavel', args=[self.pessoa.pk]),
			{'responsavel_atual': self.comum.pk},
		)
		self.assertEqual(resp.status_code, 302)
		self.pessoa.refresh_from_db()
		self.assertEqual(self.pessoa.responsavel_atual, self.comum)
		self.assertTrue(
			self.pessoa.interacoes.filter(descricao__icontains='Responsavel alterado').exists()
		)

	def test_staff_remove_responsavel(self):
		self.pessoa.responsavel_atual = self.comum
		self.pessoa.save(update_fields=['responsavel_atual'])
		self.client.force_login(self.staff)
		resp = self.client.post(
			reverse('pessoas-responsavel', args=[self.pessoa.pk]),
			{'responsavel_atual': ''},
		)
		self.assertEqual(resp.status_code, 302)
		self.pessoa.refresh_from_db()
		self.assertIsNone(self.pessoa.responsavel_atual)

	def test_usuario_comum_nao_pode_alterar_responsavel(self):
		self.client.force_login(self.comum)
		resp = self.client.post(
			reverse('pessoas-responsavel', args=[self.pessoa.pk]),
			{'responsavel_atual': self.comum.pk},
		)
		self.assertEqual(resp.status_code, 403)
		self.pessoa.refresh_from_db()
		self.assertIsNone(self.pessoa.responsavel_atual)

	def test_detalhe_mostra_form_apenas_para_staff(self):
		self.client.force_login(self.staff)
		resp = self.client.get(reverse('pessoas-detalhe', args=[self.pessoa.pk]))
		self.assertContains(resp, 'Responsavel atual')
		self.assertContains(resp, 'Associar / trocar responsavel')

		self.client.force_login(self.comum)
		resp = self.client.get(reverse('pessoas-detalhe', args=[self.pessoa.pk]))
		self.assertContains(resp, 'Responsavel atual')
		self.assertNotContains(resp, 'Associar / trocar responsavel')

	def test_filtro_meus_contatos(self):
		PrimeiroContato.objects.create(
			nome='Minha Pessoa',
			telefone_whatsapp='31955550002',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			responsavel_atual=self.comum,
		)
		self.client.force_login(self.comum)
		resp = self.client.get(reverse('pessoas-lista'), {'meus': '1'})
		self.assertEqual(resp.status_code, 200)
		nomes = [p.nome for p in resp.context['pessoas']]
		self.assertIn('Minha Pessoa', nomes)
		self.assertNotIn('Contato Um', nomes)


class FilaConfiabilidadeTests(TestCase):
	"""Sprint 2: claim atomico da fila + recuperacao de execucoes presas."""

	def setUp(self):
		self.user = get_user_model().objects.create_user(username='fila', password='x', is_staff=True)
		self.pessoa = PrimeiroContato.objects.create(
			nome='Visitante',
			telefone_whatsapp='31955554444',
			primeira_vez=True,
			como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
			o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
			status=PrimeiroContato.StatusAcolhimento.ROBO,
			iniciou_interacao=False,
		)

	def _msg_pendente(self, **extra):
		return MensagemContato.objects.create(
			pessoa=self.pessoa,
			criado_por=self.user,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='Ola',
			**extra,
		)

	def test_reivindicar_mensagem_pendente_assume_e_incrementa(self):
		from apps.acolhimento.fila_processor import _reivindicar_mensagem
		msg = self._msg_pendente()
		self.assertTrue(_reivindicar_mensagem(msg))
		msg.refresh_from_db()
		self.assertEqual(msg.status_fila, MensagemContato.StatusFilaChoices.PROCESSANDO)
		self.assertEqual(msg.tentativas_envio, 1)

	def test_reivindicar_mensagem_ja_assumida_retorna_false(self):
		from apps.acolhimento.fila_processor import _reivindicar_mensagem
		msg = self._msg_pendente()
		# Outro worker ja assumiu (PENDENTE -> PROCESSANDO) direto no banco.
		MensagemContato.objects.filter(pk=msg.pk).update(
			status_fila=MensagemContato.StatusFilaChoices.PROCESSANDO
		)
		self.assertFalse(_reivindicar_mensagem(msg))
		msg.refresh_from_db()
		self.assertEqual(msg.tentativas_envio, 0)

	def test_processador_envia_uma_vez_reivindicando_antes(self):
		# Mensagem de opt-in (nao bloqueada) chega ao envio; o gateway e mockado.
		msg = self._msg_pendente(metadata_envio={'tipo_template': 'primeiro_contato_opt_in'})
		envelope = {
			'provider': 'twilio',
			'status_fila': MensagemContato.StatusFilaChoices.ENVIADA,
			'enviada_em': timezone.now(),
			'referencia_externa': 'SMFAKE',
			'status_label': 'sent',
			'metadata_key': 'twilio',
			'metadata_value': {'sid': 'SMFAKE'},
		}
		with patch('apps.acolhimento.whatsapp_gateway.enviar', return_value=envelope) as mock_enviar:
			resultado = processar_fila_mensagens(limit=5)
		mock_enviar.assert_called_once()
		msg.refresh_from_db()
		self.assertEqual(resultado['sucesso'], 1)
		self.assertEqual(msg.status_fila, MensagemContato.StatusFilaChoices.ENVIADA)
		self.assertEqual(msg.tentativas_envio, 1)

	def test_processador_nao_envia_quando_perde_a_corrida(self):
		# Se o claim falhar (outro worker assumiu), nao chama o gateway.
		self._msg_pendente(metadata_envio={'tipo_template': 'primeiro_contato_opt_in'})
		with patch('apps.acolhimento.fila_processor._reivindicar_mensagem', return_value=False):
			with patch('apps.acolhimento.whatsapp_gateway.enviar') as mock_enviar:
				resultado = processar_fila_mensagens(limit=5)
		mock_enviar.assert_not_called()
		self.assertEqual(resultado['sucesso'], 0)

	def test_recuperar_execucoes_presas_marca_interrompida(self):
		from apps.acolhimento import fila_auto
		from apps.acolhimento.models import ExecucaoProcessamentoFila
		execucao = ExecucaoProcessamentoFila.objects.create(
			status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO,
			limite=10,
		)
		# Simula falta de progresso: atualizado_em antigo (auto_now impede via create).
		ExecucaoProcessamentoFila.objects.filter(pk=execucao.pk).update(
			atualizado_em=timezone.now() - timedelta(minutes=30)
		)
		recuperadas = fila_auto.recuperar_execucoes_presas(timeout_minutos=15)
		execucao.refresh_from_db()
		self.assertEqual(recuperadas, 1)
		self.assertEqual(execucao.status, ExecucaoProcessamentoFila.StatusExecucaoChoices.INTERROMPIDA)
		self.assertIsNotNone(execucao.finalizado_em)

	def test_recuperar_ignora_execucao_com_progresso_recente(self):
		from apps.acolhimento import fila_auto
		from apps.acolhimento.models import ExecucaoProcessamentoFila
		execucao = ExecucaoProcessamentoFila.objects.create(
			status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO,
			limite=10,
		)
		recuperadas = fila_auto.recuperar_execucoes_presas(timeout_minutos=15)
		execucao.refresh_from_db()
		self.assertEqual(recuperadas, 0)
		self.assertEqual(execucao.status, ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO)


def _pessoa(nome='Maria', telefone='31999990000', **extra):
	return PrimeiroContato.objects.create(
		nome=nome,
		telefone_whatsapp=telefone,
		primeira_vez=True,
		como_conheceu=PrimeiroContato.ComoConheceuChoices.INSTAGRAM,
		o_que_busca=PrimeiroContato.OQueBuscaChoices.CONHECER_DEUS,
		**extra,
	)


class VariacaoMensagemTests(TestCase):
	"""Variacao automatica do texto (anti-bloqueio): sem duas mensagens byte a byte iguais."""

	def test_substitui_nome_e_remove_placeholder(self):
		texto = mensagem_variacao.variar_texto('Ola {nome}!', nome='Maria', seed=1, hora=10)
		self.assertIn('Maria', texto)
		self.assertNotIn('{nome}', texto)

	def test_sem_nome_usa_fallback(self):
		texto = mensagem_variacao.variar_texto('Ola {nome}!', nome='', seed=1, hora=10)
		self.assertIn('amigo(a)', texto)

	def test_deterministico_por_seed(self):
		base = 'Ola {nome}! Podemos conversar?'
		a = mensagem_variacao.variar_texto(base, nome='Ana', seed=42, hora=9)
		b = mensagem_variacao.variar_texto(base, nome='Ana', seed=42, hora=9)
		self.assertEqual(a, b)

	def test_seeds_diferentes_geram_variacoes(self):
		base = 'Ola {nome}! Podemos conversar?'
		variantes = {
			mensagem_variacao.variar_texto(base, nome='amigo(a)', seed=s, hora=9)
			for s in range(30)
		}
		self.assertGreater(len(variantes), 1)

	def test_saudacao_inicial_varia(self):
		saudacoes = {
			mensagem_variacao.variar_texto('Ola {nome}!', nome='Jo', seed=s, hora=9).split()[0]
			for s in range(30)
		}
		self.assertGreater(len(saudacoes), 1)


class EscalonamentoAgendamentoTests(TestCase):
	"""Staggering: o disparo em massa distribui `agendada_para` no tempo."""

	def setUp(self):
		self.pessoa = _pessoa(telefone='31999990001')
		cfg = ConfiguracaoProcessamentoFila.carregar()
		cfg.intervalo_min_seg = 120
		cfg.intervalo_max_seg = 120
		cfg.janela_envio_inicio = 0
		cfg.janela_envio_fim = 24
		cfg.save()

	def test_agendamento_crescente_e_espacado(self):
		msgs = [
			MensagemContato(
				pessoa=self.pessoa,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
				conteudo=f'msg {i}',
			)
			for i in range(5)
		]
		fila_auto.escalonar_agendamento(msgs)
		tempos = [m.agendada_para for m in msgs]
		self.assertTrue(all(t is not None for t in tempos))
		for anterior, atual in zip(tempos, tempos[1:]):
			self.assertEqual((atual - anterior).total_seconds(), 120)


@override_settings(WHATSAPP_PROVIDER='evolution')
class FilaVencidasTests(TestCase):
	"""O processador so seleciona mensagens ja vencidas (agendada_para nulo ou <= agora)."""

	def setUp(self):
		self.pessoa = _pessoa(telefone='31988887777')

	def _msg(self, agendada_para):
		return MensagemContato.objects.create(
			pessoa=self.pessoa,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='oi',
			agendada_para=agendada_para,
		)

	def test_so_seleciona_vencidas(self):
		agora = timezone.now()
		self._msg(agora - timedelta(minutes=1))  # vencida
		self._msg(agora + timedelta(hours=2))    # futura (nao deve entrar)
		self._msg(None)                          # sem agendamento = imediata
		resultado = processar_fila_mensagens(dry_run=True)
		self.assertEqual(resultado['total_selecionado'], 2)


@override_settings(WHATSAPP_PROVIDER='evolution')
class TetoDiarioTests(TestCase):
	"""Teto diario interrompe a rodada ao atingir o limite (warm-up de numero novo)."""

	def setUp(self):
		self.pessoa = _pessoa(telefone='31977776666')
		cfg = ConfiguracaoProcessamentoFila.carregar()
		cfg.teto_diario = 1
		cfg.save()

	def test_para_ao_atingir_teto(self):
		for _ in range(3):
			MensagemContato.objects.create(
				pessoa=self.pessoa,
				canal=MensagemContato.CanalChoices.WHATSAPP,
				direcao=MensagemContato.DirecaoChoices.SAIDA,
				status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
				conteudo='oi',
			)
		envelope = {
			'provider': 'evolution',
			'status_fila': MensagemContato.StatusFilaChoices.ENVIADA,
			'enviada_em': timezone.now(),
			'referencia_externa': 'ref-1',
			'status_label': 'PENDING',
			'metadata_key': 'evolution',
			'metadata_value': {'sid': 'ref-1'},
		}
		with patch('apps.acolhimento.fila_processor.gateway.enviar', return_value=envelope):
			resultado = processar_fila_mensagens()
		self.assertEqual(resultado['sucesso'], 1)
		self.assertEqual(
			MensagemContato.objects.filter(status_fila=MensagemContato.StatusFilaChoices.ENVIADA).count(),
			1,
		)
		self.assertEqual(
			MensagemContato.objects.filter(status_fila=MensagemContato.StatusFilaChoices.PENDENTE).count(),
			2,
		)


class EvolutionDelayPayloadTests(TestCase):
	"""O envio pela Evolution inclui `delay` (mostra 'digitando...') quando informado."""

	def test_inclui_delay_no_payload(self):
		capturado = {}

		def fake_post(path, payload, timeout=None):
			capturado['payload'] = payload
			return 201, {'key': {'id': 'MSG123'}, 'status': 'PENDING'}

		with patch('apps.acolhimento.evolution_service._post', side_effect=fake_post):
			evolution_service.send_whatsapp_text(to_phone='5531999998888', text='oi', delay_ms=1500)
		self.assertEqual(capturado['payload'].get('delay'), 1500)
		self.assertIn('number', capturado['payload'])
		self.assertEqual(capturado['payload'].get('text'), 'oi')

	def test_sem_delay_quando_nulo(self):
		capturado = {}

		def fake_post(path, payload, timeout=None):
			capturado['payload'] = payload
			return 201, {'key': {'id': 'MSG123'}}

		with patch('apps.acolhimento.evolution_service._post', side_effect=fake_post):
			evolution_service.send_whatsapp_text(to_phone='5531999998888', text='oi', delay_ms=None)
		self.assertNotIn('delay', capturado['payload'])


@override_settings(WHATSAPP_PROVIDER='evolution')
class GatewayEvolutionEnvioTests(TestCase):
	"""Integracao: o envio opt-in varia o texto e repassa o delay de 'digitando...'."""

	def test_envio_opt_in_varia_texto_e_passa_delay(self):
		pessoa = _pessoa(nome='Bruna', telefone='31999991111')
		msg = MensagemContato.objects.create(
			pessoa=pessoa,
			canal=MensagemContato.CanalChoices.WHATSAPP,
			direcao=MensagemContato.DirecaoChoices.SAIDA,
			status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
			conteudo='x',
			metadata_envio={
				'tipo_template': 'primeiro_contato_opt_in',
				'evolution_texto': 'Ola {nome}! Podemos conversar?',
			},
		)
		capturado = {}

		def fake_send(*, to_phone, text, delay_ms=None):
			capturado['text'] = text
			capturado['delay_ms'] = delay_ms
			return {'sid': 'X', 'status': 'PENDING', 'to': to_phone, 'raw': {}}

		with patch('apps.acolhimento.evolution_service.send_whatsapp_text', side_effect=fake_send):
			envelope = whatsapp_gateway.enviar(msg, '5531999991111')
		self.assertIn('Bruna', capturado['text'])
		self.assertNotIn('{nome}', capturado['text'])
		self.assertGreater(capturado['delay_ms'], 0)
		self.assertEqual(envelope['status_fila'], MensagemContato.StatusFilaChoices.ENVIADA)
