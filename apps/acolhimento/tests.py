from datetime import time, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from apps.acolhimento.fila_processor import processar_fila_mensagens
from apps.acolhimento.forms import AutoCadastroPrimeiroContatoForm, PerguntaForm, PrimeiroContatoForm, RelatorioPessoasForm, ResponderQuestionarioForm
from apps.acolhimento.models import (
	ConviteQuestionario,
	InteracaoAcolhimento,
	MensagemContato,
	OpcaoPergunta,
	PerguntaQuestionario,
	PrimeiroContato,
	Questionario,
)
from apps.acolhimento.views import (
	PERMISSAO_CONVERSAR_PESSOAS,
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
	def test_menu_configuracoes_aparece_somente_para_superuser(self):
		self.client.force_login(self.staff_admin)
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.status_code, 200)
		self.assertNotContains(resp, 'settings-navigation')
		self.assertNotContains(resp, 'Usuarios')
		self.assertNotContains(resp, 'Processar fila')
		self.assertNotContains(resp, 'Questionarios')

		self.client.force_login(self.admin)
		resp = self.client.get(reverse('dashboard'))
		self.assertEqual(resp.status_code, 200)
		self.assertContains(resp, 'settings-navigation')
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
