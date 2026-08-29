import json

from django import forms
from django.utils import timezone

from apps.acolhimento import reports
from apps.acolhimento.models import (
    ConfiguracaoAtendimentoBot,
    ConviteQuestionario,
    InteracaoAcolhimento,
    MensagemContato,
    OpcaoAtendimentoBot,
    OpcaoPergunta,
    PerguntaQuestionario,
    PrimeiroContato,
    Questionario,
    RespostaPergunta,
    hora_local_atual,
)
from apps.acolhimento.phone_utils import find_pessoa_by_phone


class TelefoneWhatsappUnicoMixin:
    """Impede cadastrar uma pessoa com um numero de WhatsApp ja existente.

    A comparacao considera variacoes de formato do mesmo numero (com/sem
    DDI 55, com/sem o nono digito, com ou sem mascara).
    """

    revelar_nome_duplicado = False

    def clean_telefone_whatsapp(self):
        telefone = self.cleaned_data['telefone_whatsapp']
        exclude_pk = self.instance.pk if self.instance and self.instance.pk else None
        pessoa_existente = find_pessoa_by_phone(telefone, exclude_pk=exclude_pk)

        if pessoa_existente is not None:
            if self.revelar_nome_duplicado:
                raise forms.ValidationError(
                    f'Já existe um cadastro com este número de WhatsApp ({pessoa_existente.nome}).'
                )
            raise forms.ValidationError('Este número de WhatsApp já está cadastrado.')

        return telefone


class PrimeiroContatoForm(TelefoneWhatsappUnicoMixin, forms.ModelForm):
    revelar_nome_duplicado = True

    primeira_vez = forms.TypedChoiceField(
        label='Primeira vez?',
        choices=((True, 'Sim'), (False, 'Nao')),
        coerce=lambda value: value in (True, 'True', 'true', '1', 1),
        widget=forms.Select,
    )

    class Meta:
        model = PrimeiroContato
        fields = [
            'nome',
            'telefone_whatsapp',
            'primeira_vez',
            'como_conheceu',
            'o_que_busca',
        ]


class AutoCadastroPrimeiroContatoForm(TelefoneWhatsappUnicoMixin, forms.ModelForm):
    primeira_vez = forms.TypedChoiceField(
        label='Primeira vez?',
        choices=((True, 'Sim'), (False, 'Nao')),
        coerce=lambda value: value in (True, 'True', 'true', '1', 1),
        widget=forms.Select,
    )

    class Meta:
        model = PrimeiroContato
        fields = [
            'nome',
            'telefone_whatsapp',
            'primeira_vez',
            'como_conheceu',
            'o_que_busca',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Opcionais para agilizar o auto cadastro quando ha um grande fluxo de pessoas.
        self.fields['como_conheceu'].required = False
        self.fields['o_que_busca'].required = False


class PrimeiroContatoAdminForm(TelefoneWhatsappUnicoMixin, forms.ModelForm):
    revelar_nome_duplicado = True

    class Meta:
        model = PrimeiroContato
        fields = '__all__'


class InteracaoAcolhimentoForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.fields['data_interacao'].initial = timezone.localdate
            self.fields['hora_interacao'].initial = hora_local_atual

    class Meta:
        model = InteracaoAcolhimento
        fields = ['tipo', 'data_interacao', 'hora_interacao', 'descricao']
        widgets = {
            'data_interacao': forms.DateInput(attrs={'type': 'date'}),
            'hora_interacao': forms.TimeInput(attrs={'type': 'time'}),
            'descricao': forms.Textarea(attrs={'rows': 3}),
        }


class EnfileirarMensagemForm(forms.ModelForm):
    class Meta:
        model = MensagemContato
        # Canal fixo em WhatsApp por enquanto (opcao de e-mail desativada).
        # Para reativar o e-mail, adicione 'canal' de volta aos fields.
        fields = ['conteudo']
        widgets = {
            'conteudo': forms.Textarea(attrs={
                'rows': 2,
                'placeholder': 'Escreva sua mensagem para o WhatsApp...',
                'aria-label': 'Mensagem',
            }),
        }


class DisparoMensagemMassaForm(forms.Form):
    """Dois modos de disparo: mensagem livre (janela 24h aberta) ou template de marketing."""

    MODO_LIVRE = 'livre'
    MODO_TEMPLATE = 'template'
    MODO_CHOICES = [
        (MODO_LIVRE, 'Mensagem livre — so para quem esta com a janela de 24h aberta'),
        (MODO_TEMPLATE, 'Template — para qualquer pessoa fora do primeiro contato (marketing)'),
    ]

    modo = forms.ChoiceField(
        choices=MODO_CHOICES,
        initial=MODO_LIVRE,
        widget=forms.RadioSelect,
    )
    conteudo = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 5, 'placeholder': 'Escreva a mensagem que sera enviada...'}),
    )
    content_sid = forms.CharField(
        required=False,
        label='Content Template SID',
        widget=forms.TextInput(attrs={'placeholder': 'HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'}),
    )
    content_variables = forms.CharField(
        required=False,
        label='Variaveis do template (JSON)',
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': '{"1": "{nome}"}'}),
    )
    pessoas = forms.ModelMultipleChoiceField(
        queryset=PrimeiroContato.objects.none(),
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        pessoas_queryset = kwargs.pop('pessoas_queryset', None)
        super().__init__(*args, **kwargs)
        if pessoas_queryset is None:
            pessoas_queryset = PrimeiroContato.objects.order_by('nome')
        self.fields['pessoas'].queryset = pessoas_queryset

    def clean_content_variables(self):
        raw = (self.cleaned_data.get('content_variables') or '').strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise forms.ValidationError('JSON invalido. Ex.: {"1": "{nome}"}.')
        if not isinstance(parsed, dict):
            raise forms.ValidationError('Use um objeto JSON, ex.: {"1": "{nome}"}.')
        return parsed

    def clean(self):
        cleaned = super().clean()
        modo = cleaned.get('modo')
        if modo == self.MODO_TEMPLATE:
            sid = (cleaned.get('content_sid') or '').strip()
            if not sid:
                self.add_error('content_sid', 'Informe o Content Template SID para o disparo por template.')
            elif not sid.startswith('HX'):
                self.add_error('content_sid', 'O Content SID da Twilio comeca com "HX".')
        else:
            if not (cleaned.get('conteudo') or '').strip():
                self.add_error('conteudo', 'Escreva a mensagem que sera enviada.')
        return cleaned


class RelatorioPessoasForm(forms.Form):
    FORMATO_CHOICES = [
        ('pdf', 'PDF'),
        ('xlsx', 'Excel (.xlsx)'),
        ('csv', 'CSV'),
    ]

    q = forms.CharField(
        required=False,
        label='Busca',
        widget=forms.TextInput(attrs={'placeholder': 'Nome, WhatsApp, e-mail ou cidade'}),
    )
    status = forms.ChoiceField(
        required=False,
        label='Status',
        choices=[('', 'Todos')] + list(PrimeiroContato.StatusAcolhimento.choices),
    )
    origem = forms.ChoiceField(
        required=False,
        label='Origem',
        choices=[('', 'Todas')] + list(PrimeiroContato.OrigemCadastroChoices.choices),
    )
    primeira_vez = forms.ChoiceField(required=False, label='Primeira vez', choices=reports.TRIESTADO_CHOICES)
    iniciou_interacao = forms.ChoiceField(required=False, label='Iniciou interacao', choices=reports.TRIESTADO_CHOICES)
    data_inicio = forms.DateField(
        required=False,
        label='Primeiro contato de',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    data_fim = forms.DateField(
        required=False,
        label='ate',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    responsavel = forms.ChoiceField(required=False, label='Responsavel', choices=[('', 'Todos')])
    colunas = forms.MultipleChoiceField(
        label='Colunas para exportar',
        required=False,
        choices=reports.colunas_choices(),
        widget=forms.CheckboxSelectMultiple,
        initial=reports.COLUNAS_PADRAO,
    )
    formato = forms.ChoiceField(
        label='Formato',
        choices=FORMATO_CHOICES,
        initial='xlsx',
        widget=forms.RadioSelect,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['responsavel'].choices = reports.responsavel_choices()

    def clean_colunas(self):
        colunas = reports.normalizar_colunas(self.cleaned_data.get('colunas'))
        if not colunas:
            raise forms.ValidationError('Selecione ao menos uma coluna para exportar.')
        return colunas

    def clean(self):
        cleaned_data = super().clean()
        inicio = cleaned_data.get('data_inicio')
        fim = cleaned_data.get('data_fim')
        if inicio and fim and inicio > fim:
            self.add_error('data_fim', 'A data final deve ser maior ou igual a data inicial.')
        return cleaned_data


class QuestionarioForm(forms.ModelForm):
    class Meta:
        model = Questionario
        fields = ['titulo', 'descricao', 'ativo', 'padrao_membresia']
        widgets = {
            'descricao': forms.Textarea(attrs={'rows': 3}),
        }


class PerguntaForm(forms.ModelForm):
    opcoes_texto = forms.CharField(
        required=False,
        label='Opcoes (uma por linha)',
        widget=forms.Textarea(attrs={
            'rows': 4,
            'placeholder': 'Uma opcao por linha. So usado quando o tipo e "Escolha unica".',
        }),
    )

    class Meta:
        model = PerguntaQuestionario
        fields = ['texto', 'ajuda', 'tipo', 'obrigatoria']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['opcoes_texto'].initial = '\n'.join(
                self.instance.opcoes.values_list('texto', flat=True)
            )

    def clean(self):
        cleaned_data = super().clean()
        opcoes = [linha.strip() for linha in (cleaned_data.get('opcoes_texto') or '').splitlines() if linha.strip()]
        if cleaned_data.get('tipo') == PerguntaQuestionario.TipoPergunta.ESCOLHA_UNICA and not opcoes:
            self.add_error('opcoes_texto', 'Informe ao menos uma opcao para perguntas de escolha unica.')
        cleaned_data['opcoes_lista'] = opcoes
        return cleaned_data

    def save(self, commit=True):
        pergunta = super().save(commit=commit)
        if commit:
            self.salvar_opcoes(pergunta)
        return pergunta

    def salvar_opcoes(self, pergunta):
        pergunta.opcoes.all().delete()
        if pergunta.tipo == PerguntaQuestionario.TipoPergunta.ESCOLHA_UNICA:
            OpcaoPergunta.objects.bulk_create([
                OpcaoPergunta(pergunta=pergunta, texto=texto, ordem=indice)
                for indice, texto in enumerate(self.cleaned_data.get('opcoes_lista', []))
            ])


class ResponderQuestionarioForm(forms.Form):
    respondente_nome = forms.CharField(
        label='Quem esta preenchendo?',
        max_length=150,
        widget=forms.TextInput(attrs={'placeholder': 'Seu nome (ou de quem preenche)'}),
    )

    def __init__(self, *args, questionario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.perguntas = list(questionario.perguntas.prefetch_related('opcoes')) if questionario else []
        for pergunta in self.perguntas:
            self.fields[f'pergunta_{pergunta.id}'] = self._construir_campo(pergunta)

    def _construir_campo(self, pergunta):
        Tipo = PerguntaQuestionario.TipoPergunta
        comum = {'label': pergunta.texto, 'help_text': pergunta.ajuda, 'required': pergunta.obrigatoria}

        if pergunta.tipo == Tipo.TEXTO_LONGO:
            return forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), **comum)
        if pergunta.tipo == Tipo.DATA:
            return forms.DateField(widget=forms.DateInput(attrs={'type': 'date'}), **comum)
        if pergunta.tipo == Tipo.SIM_NAO:
            return forms.ChoiceField(choices=[('Sim', 'Sim'), ('Nao', 'Nao')], widget=forms.RadioSelect, **comum)
        if pergunta.tipo == Tipo.ESCOLHA_UNICA:
            choices = [(str(opcao.id), opcao.texto) for opcao in pergunta.opcoes.all()]
            return forms.ChoiceField(choices=choices, widget=forms.RadioSelect, **comum)
        return forms.CharField(**comum)

    def campos_perguntas(self):
        for pergunta in self.perguntas:
            yield pergunta, self[f'pergunta_{pergunta.id}']

    def salvar_respostas(self, convite):
        Tipo = PerguntaQuestionario.TipoPergunta
        for pergunta in self.perguntas:
            valor = self.cleaned_data.get(f'pergunta_{pergunta.id}')
            if valor in (None, ''):
                continue
            resposta = RespostaPergunta(convite=convite, pergunta=pergunta)
            if pergunta.tipo == Tipo.ESCOLHA_UNICA:
                resposta.opcao_id = int(valor)
            elif pergunta.tipo == Tipo.DATA:
                resposta.valor_texto = valor.strftime('%d/%m/%Y') if hasattr(valor, 'strftime') else str(valor)
            else:
                resposta.valor_texto = str(valor)
            resposta.save()


class TemplatesWhatsappForm(forms.Form):
    """Edita os templates Twilio e os textos equivalentes do WhatsApp."""

    opt_in_sid = forms.CharField(
        required=False,
        label='Primeiro contato — Content Template SID',
        widget=forms.TextInput(attrs={'placeholder': 'HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'}),
    )
    opt_in_variables = forms.CharField(
        required=False,
        label='Primeiro contato — Variaveis (JSON)',
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': '{"1": "{nome}"}'}),
    )
    opt_in_texto_evolution = forms.CharField(
        required=False,
        label='Primeiro contato — Mensagem de boas-vindas',
        widget=forms.Textarea(attrs={
            'rows': 4,
            'placeholder': 'Ola {nome}! Aqui e o acolhimento da PIBVP. Podemos conversar por aqui?',
        }),
    )
    continuar_sid = forms.CharField(
        required=False,
        label='Continuar conversa — Content Template SID',
        widget=forms.TextInput(attrs={'placeholder': 'HXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'}),
    )
    continuar_variables = forms.CharField(
        required=False,
        label='Continuar conversa — Variaveis (JSON)',
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': '{}'}),
    )
    continuar_texto_evolution = forms.CharField(
        required=False,
        label='Continuar conversa — Mensagem de continuacao',
        widget=forms.Textarea(attrs={
            'rows': 4,
            'placeholder': 'Ola {nome}, tudo bem? Podemos continuar nossa conversa?',
        }),
    )

    def clean(self):
        cleaned = super().clean()
        for campo in ('opt_in_sid', 'continuar_sid'):
            sid = (cleaned.get(campo) or '').strip()
            if sid and not sid.startswith('HX'):
                self.add_error(campo, 'O Content SID da Twilio comeca com "HX".')
            cleaned[campo] = sid
        for campo in ('opt_in_variables', 'continuar_variables'):
            raw = (cleaned.get(campo) or '').strip() or '{}'
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError
            except ValueError:
                self.add_error(campo, 'JSON invalido. Use um objeto, ex.: {"1": "{nome}"}.')
            cleaned[campo] = raw
        for campo in ('opt_in_texto_evolution', 'continuar_texto_evolution'):
            cleaned[campo] = (cleaned.get(campo) or '').strip()
        return cleaned


class ConfiguracaoAtendimentoBotForm(forms.ModelForm):
    """Textos fixos do atendimento automatico (o liga/desliga fica num botao a parte)."""

    class Meta:
        model = ConfiguracaoAtendimentoBot
        fields = ['mensagem_saudacao', 'mensagem_fallback']
        widgets = {
            'mensagem_saudacao': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': 'Ola! Que bom falar com voce. Com o que posso te ajudar hoje?',
            }),
            'mensagem_fallback': forms.Textarea(attrs={
                'rows': 2,
                'placeholder': 'Nao entendi. Responda com o numero de uma das opcoes abaixo:',
            }),
        }


class OpcaoAtendimentoBotForm(forms.ModelForm):
    """Uma opcao do menu do atendimento automatico."""

    class Meta:
        model = OpcaoAtendimentoBot
        fields = ['rotulo', 'palavras_chave', 'resposta', 'acao', 'ativa']
        widgets = {
            'rotulo': forms.TextInput(attrs={'placeholder': 'Ex.: Falar com alguem'}),
            'palavras_chave': forms.Textarea(attrs={
                'rows': 2,
                'placeholder': 'atendente, falar, pessoa (o numero da opcao sempre funciona)',
            }),
            'resposta': forms.Textarea(attrs={
                'rows': 4,
                'placeholder': 'Texto que o bot envia quando a pessoa escolhe esta opcao.',
            }),
        }
