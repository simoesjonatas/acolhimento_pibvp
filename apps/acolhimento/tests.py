from django.test import TestCase

from apps.acolhimento.forms import AutoCadastroPrimeiroContatoForm, PrimeiroContatoForm
from apps.acolhimento.models import PrimeiroContato


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
