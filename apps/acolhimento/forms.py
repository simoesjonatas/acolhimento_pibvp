from django import forms

from apps.acolhimento import reports
from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato
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
                    f'Ja existe um cadastro com este numero de WhatsApp ({pessoa_existente.nome}).'
                )
            raise forms.ValidationError('Este numero de WhatsApp ja esta cadastrado.')

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


class PrimeiroContatoAdminForm(TelefoneWhatsappUnicoMixin, forms.ModelForm):
    revelar_nome_duplicado = True

    class Meta:
        model = PrimeiroContato
        fields = '__all__'


class InteracaoAcolhimentoForm(forms.ModelForm):
    class Meta:
        model = InteracaoAcolhimento
        fields = ['tipo', 'data_interacao', 'descricao']
        widgets = {
            'data_interacao': forms.DateInput(attrs={'type': 'date'}),
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
    canal = forms.ChoiceField(choices=MensagemContato.CanalChoices.choices)
    conteudo = forms.CharField(widget=forms.Textarea(attrs={'rows': 5}))
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
