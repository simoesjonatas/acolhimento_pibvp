from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Permission

from apps.core.models import QrCodeDinamico


User = get_user_model()
PERMISSAO_CONVERSAR_PESSOAS = 'acolhimento.pode_conversar_pessoas'


def get_conversar_pessoas_permission():
    return Permission.objects.filter(
        codename='pode_conversar_pessoas',
        content_type__app_label='acolhimento',
        content_type__model='primeirocontato',
    ).first()


class LoginForm(AuthenticationForm):
    """Formulario de login que aceita usuario ou e-mail e evita capitalizacao."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = 'Usuário ou e-mail'
        self.fields['username'].widget.attrs.update({
            'autofocus': True,
            'autocapitalize': 'none',
            'autocomplete': 'username',
            'spellcheck': 'false',
        })


class UsernameMinusculoMixin:
    """Normaliza o username para minusculo (a unicidade e checada ja em minusculo)."""

    def clean_username(self):
        return (self.cleaned_data.get('username') or '').strip().lower()


class UsuarioPermissoesFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        permission_field = forms.BooleanField(
            label='Mensagens das pessoas',
            required=False,
        )
        fields = self.fields.__class__()
        for field_name, field in self.fields.items():
            fields[field_name] = field
            if field_name == 'is_active':
                fields['pode_conversar_pessoas'] = permission_field
        self.fields = fields

        instance = getattr(self, 'instance', None)
        if instance and instance.pk:
            self.fields['pode_conversar_pessoas'].initial = instance.has_perm(PERMISSAO_CONVERSAR_PESSOAS)

    def salvar_permissao_conversas(self, user):
        permission = get_conversar_pessoas_permission()
        if not permission:
            return

        if self.cleaned_data.get('pode_conversar_pessoas'):
            user.user_permissions.add(permission)
        else:
            user.user_permissions.remove(permission)


class UsuarioCreateForm(UsuarioPermissoesFormMixin, UsernameMinusculoMixin, forms.ModelForm):
    password1 = forms.CharField(label='Senha', widget=forms.PasswordInput)
    password2 = forms.CharField(label='Confirmar senha', widget=forms.PasswordInput)

    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'is_active', 'is_staff']

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('password1')
        password2 = cleaned_data.get('password2')

        if password1 and password2 and password1 != password2:
            self.add_error('password2', 'As senhas nao conferem.')

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data['password1'])
        if commit:
            user.save()
            self.salvar_permissao_conversas(user)
        return user


class UsuarioUpdateForm(UsuarioPermissoesFormMixin, UsernameMinusculoMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ['username', 'first_name', 'last_name', 'email', 'is_active', 'is_staff']

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            self.salvar_permissao_conversas(user)
        return user


class PerfilForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']


class QrCodeDinamicoForm(forms.ModelForm):
    """Cadastro/edicao de um QR Code dinamico (link fixo, destino reconfiguravel)."""

    class Meta:
        model = QrCodeDinamico
        fields = ['nome', 'destino', 'ativo', 'descricao']
        widgets = {
            'nome': forms.TextInput(attrs={
                'placeholder': 'Ex.: Cartaz da entrada',
                'autofocus': True,
            }),
            'destino': forms.URLInput(attrs={
                'placeholder': 'https://...',
                'autocapitalize': 'none',
                'spellcheck': 'false',
            }),
            'descricao': forms.Textarea(attrs={
                'rows': 3,
                'placeholder': 'Anotacoes internas (opcional).',
            }),
        }

    def clean_destino(self):
        return (self.cleaned_data.get('destino') or '').strip()
