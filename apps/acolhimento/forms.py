from django import forms

from apps.acolhimento.models import InteracaoAcolhimento, PrimeiroContato


class PrimeiroContatoForm(forms.ModelForm):
    class Meta:
        model = PrimeiroContato
        fields = [
            'nome',
            'telefone',
            'email',
            'cidade',
            'como_conheceu',
            'observacoes',
        ]
        widgets = {
            'observacoes': forms.Textarea(attrs={'rows': 4}),
        }


class InteracaoAcolhimentoForm(forms.ModelForm):
    class Meta:
        model = InteracaoAcolhimento
        fields = ['tipo', 'data_interacao', 'descricao']
        widgets = {
            'data_interacao': forms.DateInput(attrs={'type': 'date'}),
            'descricao': forms.Textarea(attrs={'rows': 3}),
        }
