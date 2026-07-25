from django import forms

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
        fields = ['canal', 'conteudo']
        widgets = {
            'conteudo': forms.Textarea(attrs={'rows': 4}),
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
