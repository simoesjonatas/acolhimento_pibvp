from django import forms

from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato


class PrimeiroContatoForm(forms.ModelForm):
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


class AutoCadastroPrimeiroContatoForm(forms.ModelForm):
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
