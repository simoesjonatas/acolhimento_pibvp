import json
import re

from django import forms
from django.utils import timezone

from apps.acolhimento import reports
from apps.acolhimento.models import (
    TabelaPrecoWhatsapp,
    ConfiguracaoAtendimentoBot,
    ConviteQuestionario,
    InteracaoAcolhimento,
    MensagemContato,
    OpcaoAtendimentoBot,
    OpcaoComoConheceu,
    OpcaoPergunta,
    PerguntaQuestionario,
    PrimeiroContato,
    Questionario,
    RegistroGabinete,
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


class ComoConheceuOpcoesMixin:
    """Troca `como_conheceu` por um select com as opcoes ativas cadastradas pelo administrador."""

    como_conheceu_obrigatorio = True

    def _configurar_como_conheceu(self):
        atual = self.instance.como_conheceu if self.instance and self.instance.pk else ''
        choices = OpcaoComoConheceu.choices_ativas(incluir_valor=atual)
        if not choices:
            # Sem nenhuma opcao ativa nao ha o que perguntar; o modelo aceita salvar ''.
            del self.fields['como_conheceu']
            return
        self.fields['como_conheceu'] = forms.ChoiceField(
            label='Como conheceu',
            choices=[('', '---------')] + choices,
            required=self.como_conheceu_obrigatorio,
        )


class SobrenomeMixin:
    """Pede o sobrenome num campo proprio (obrigatorio) e grava o nome completo em `nome`."""

    def _configurar_sobrenome(self):
        self.fields['sobrenome'] = forms.CharField(label='Sobrenome', max_length=100)
        self.order_fields(['nome', 'sobrenome'])

    def clean(self):
        cleaned_data = super().clean()
        nome = (cleaned_data.get('nome') or '').strip()
        sobrenome = (cleaned_data.get('sobrenome') or '').strip()
        if nome and sobrenome:
            # O modelo guarda o nome completo num campo so.
            nome_completo = f'{nome} {sobrenome}'
            limite = PrimeiroContato._meta.get_field('nome').max_length
            if len(nome_completo) > limite:
                self.add_error('sobrenome', f'Nome e sobrenome juntos devem ter no maximo {limite} caracteres.')
            else:
                cleaned_data['nome'] = nome_completo
        return cleaned_data


class PrimeiroContatoForm(SobrenomeMixin, ComoConheceuOpcoesMixin, TelefoneWhatsappUnicoMixin, forms.ModelForm):
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._configurar_como_conheceu()
        # O cadastro novo segue o auto cadastro: pede sobrenome e nao pergunta 'o_que_busca'
        # (o modelo aceita salvar ''). Na edicao o `nome` ja traz o nome completo e a
        # equipe pode preencher 'o_que_busca'.
        if not self.instance.pk:
            self._configurar_sobrenome()
            del self.fields['o_que_busca']
        else:
            self.fields['o_que_busca'].required = False


class AutoCadastroPrimeiroContatoForm(SobrenomeMixin, ComoConheceuOpcoesMixin, TelefoneWhatsappUnicoMixin, forms.ModelForm):
    # Opcional para agilizar o auto cadastro quando ha um grande fluxo de pessoas.
    como_conheceu_obrigatorio = False

    primeira_vez = forms.TypedChoiceField(
        label='Primeira vez?',
        choices=((True, 'Sim'), (False, 'Nao')),
        coerce=lambda value: value in (True, 'True', 'true', '1', 1),
        widget=forms.Select,
    )

    class Meta:
        model = PrimeiroContato
        # 'o_que_busca' fica fora do auto cadastro: a equipe preenche depois, no
        # cadastro interno. O modelo aceita vazio, entao salva como ''.
        fields = [
            'nome',
            'telefone_whatsapp',
            'primeira_vez',
            'como_conheceu',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._configurar_como_conheceu()
        self._configurar_sobrenome()


class PrimeiroContatoAdminForm(ComoConheceuOpcoesMixin, TelefoneWhatsappUnicoMixin, forms.ModelForm):
    revelar_nome_duplicado = True

    class Meta:
        model = PrimeiroContato
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'como_conheceu' in self.fields:
            self._configurar_como_conheceu()


class InteracaoAcolhimentoForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.fields['data_interacao'].initial = timezone.localdate
            self.fields['hora_interacao'].initial = hora_local_atual
        # Na ficha o Novo passo e uma linha so, sem rotulos visiveis: a opcao vazia explica o campo.
        self.fields['tipo'].choices = [('', 'Tipo do passo')] + list(InteracaoAcolhimento.TipoInteracao.choices)

    class Meta:
        model = InteracaoAcolhimento
        fields = ['tipo', 'data_interacao', 'hora_interacao', 'descricao']
        widgets = {
            # O <input type="date"> so aceita AAAA-MM-DD: no formato local a data padrao some.
            'data_interacao': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            # Sem segundos: com "13:30:47" de valor, o navegador so aceita horarios
            # terminados em :47 e bloqueia o envio quando a pessoa digita 19:30.
            'hora_interacao': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'descricao': forms.Textarea(attrs={'rows': 1, 'placeholder': 'Descreva o passo (opcional)'}),
        }


class RegistroGabineteForm(forms.ModelForm):
    """Caixa "Gabinete" da ficha da pessoa (so administradores e pastores)."""

    prefix = 'gabinete'  # a ficha tambem tem o form do Novo passo, com um campo `tipo`

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.fields['data'].initial = timezone.localdate
            self.fields['hora'].initial = hora_local_atual
        self.fields['tipo'].choices = [('', 'Tipo de gabinete')] + list(RegistroGabinete.Tipo.choices)

    class Meta:
        model = RegistroGabinete
        fields = ['tipo', 'data', 'hora', 'observacoes']
        widgets = {
            'data': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'hora': forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
            'observacoes': forms.Textarea(attrs={'rows': 1, 'placeholder': 'Observacoes (opcional)'}),
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
    """Tres modos de disparo: mensagem livre (janela 24h aberta), template de marketing ou
    link de questionario (cada pessoa recebe o proprio link de resposta)."""

    MODO_LIVRE = 'livre'
    MODO_TEMPLATE = 'template'
    MODO_QUESTIONARIO = 'questionario'
    MODO_CHOICES = [
        (MODO_LIVRE, 'Mensagem livre — so para quem esta com a janela de 24h aberta'),
        (MODO_TEMPLATE, 'Template — para qualquer pessoa fora do primeiro contato (marketing)'),
        (MODO_QUESTIONARIO, 'Questionario — cada pessoa recebe o proprio link de resposta'),
    ]
    TEXTO_CONVITE_PADRAO = 'Olá, {nome}! Segue o link para responder o questionário "{questionario}": {link}'

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
    # Somente na API oficial da Meta: idioma do template (o nome vai em `content_sid`).
    template_idioma = forms.CharField(
        required=False,
        label='Idioma do template',
        initial='pt_BR',
        widget=forms.TextInput(attrs={'placeholder': 'pt_BR'}),
    )
    content_variables = forms.CharField(
        required=False,
        label='Variaveis do template (JSON)',
        widget=forms.Textarea(attrs={'rows': 2, 'placeholder': '{"1": "{nome}"}'}),
    )
    questionario = forms.ModelChoiceField(
        queryset=Questionario.objects.none(),
        required=False,
        empty_label='Selecione o questionario',
    )
    texto_convite = forms.CharField(
        required=False,
        initial=TEXTO_CONVITE_PADRAO,
        widget=forms.Textarea(attrs={'rows': 4}),
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
        # Sem pergunta o link abriria um formulario vazio: so entram os que tem perguntas.
        self.fields['questionario'].queryset = (
            Questionario.objects.filter(ativo=True, perguntas__isnull=False).distinct().order_by('titulo')
        )
        membresia = Questionario.membresia()
        self.membresia_id = membresia.pk if membresia else None
        self.fields['questionario'].label_from_instance = self._rotulo_questionario

    def _rotulo_questionario(self, questionario):
        if questionario.pk == self.membresia_id:
            return f'{questionario.titulo} (so Ingressantes)'
        return questionario.titulo

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
        from apps.acolhimento import template_config  # lazy: evita ciclo de import no load

        cleaned = super().clean()
        modo = cleaned.get('modo')
        if modo == self.MODO_TEMPLATE:
            sid = (cleaned.get('content_sid') or '').strip()
            if template_config.usando_meta():
                if not sid:
                    self.add_error('content_sid', 'Informe o nome do template aprovado na Meta.')
                elif not META_TEMPLATE_NOME_RE.match(sid):
                    self.add_error('content_sid', META_TEMPLATE_NOME_ERRO)
                idioma = (cleaned.get('template_idioma') or '').strip() or 'pt_BR'
                if not META_IDIOMA_RE.match(idioma):
                    self.add_error('template_idioma', META_IDIOMA_ERRO)
                cleaned['template_idioma'] = idioma
            elif not sid:
                self.add_error('content_sid', 'Informe o Content Template SID para o disparo por template.')
            elif not sid.startswith('HX'):
                self.add_error('content_sid', 'O Content SID da Twilio comeca com "HX".')
        elif modo == self.MODO_QUESTIONARIO:
            if not cleaned.get('questionario'):
                self.add_error('questionario', 'Escolha o questionario que sera enviado.')
            texto = (cleaned.get('texto_convite') or '').strip()
            if not texto:
                self.add_error('texto_convite', 'Escreva a mensagem que acompanha o link.')
            elif '{link}' not in texto:
                self.add_error('texto_convite', 'Inclua {link} na mensagem: e ali que entra o link de cada pessoa.')
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


# Nome de template na Meta: minusculas, digitos e underscore (ate 512 chars).
META_TEMPLATE_NOME_RE = re.compile(r'^[a-z0-9_]{1,512}$')
META_TEMPLATE_NOME_ERRO = 'O nome do template na Meta usa so letras minusculas, numeros e _ (ex.: boas_vindas_pibvp).'
# Codigo de idioma da Meta: pt_BR, en_US, es, ...
META_IDIOMA_RE = re.compile(r'^[a-z]{2,3}(_[A-Z]{2})?$')
META_IDIOMA_ERRO = 'Use o codigo de idioma da Meta, ex.: pt_BR.'


class TemplatesWhatsappForm(forms.Form):
    """Edita os templates Twilio, os templates da Meta e os textos equivalentes do WhatsApp."""

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
    opt_in_meta_nome = forms.CharField(
        required=False,
        label='Primeiro contato — Nome do template (Meta)',
        widget=forms.TextInput(attrs={'placeholder': 'boas_vindas_pibvp'}),
    )
    opt_in_meta_idioma = forms.CharField(
        required=False,
        label='Primeiro contato — Idioma do template (Meta)',
        widget=forms.TextInput(attrs={'placeholder': 'pt_BR'}),
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
    continuar_meta_nome = forms.CharField(
        required=False,
        label='Continuar conversa — Nome do template (Meta)',
        widget=forms.TextInput(attrs={'placeholder': 'continuar_conversa_pibvp'}),
    )
    continuar_meta_idioma = forms.CharField(
        required=False,
        label='Continuar conversa — Idioma do template (Meta)',
        widget=forms.TextInput(attrs={'placeholder': 'pt_BR'}),
    )

    def clean(self):
        cleaned = super().clean()
        for campo in ('opt_in_sid', 'continuar_sid'):
            sid = (cleaned.get(campo) or '').strip()
            if sid and not sid.startswith('HX'):
                self.add_error(campo, 'O Content SID da Twilio comeca com "HX".')
            cleaned[campo] = sid
        for campo in ('opt_in_meta_nome', 'continuar_meta_nome'):
            nome = (cleaned.get(campo) or '').strip()
            if nome and not META_TEMPLATE_NOME_RE.match(nome):
                self.add_error(campo, META_TEMPLATE_NOME_ERRO)
            cleaned[campo] = nome
        for campo in ('opt_in_meta_idioma', 'continuar_meta_idioma'):
            idioma = (cleaned.get(campo) or '').strip() or 'pt_BR'
            if not META_IDIOMA_RE.match(idioma):
                self.add_error(campo, META_IDIOMA_ERRO)
            cleaned[campo] = idioma
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


class OpcaoComoConheceuForm(forms.ModelForm):
    """Uma opcao do campo "Como conheceu" do cadastro."""

    class Meta:
        model = OpcaoComoConheceu
        fields = ['rotulo', 'ativa']
        widgets = {
            'rotulo': forms.TextInput(attrs={'placeholder': 'Ex.: Instagram'}),
        }

    def clean_rotulo(self):
        rotulo = self.cleaned_data['rotulo'].strip()
        duplicada = OpcaoComoConheceu.objects.filter(rotulo__iexact=rotulo).exclude(pk=self.instance.pk)
        if duplicada.exists():
            raise forms.ValidationError('Ja existe uma opcao com este nome.')
        return rotulo


class PeriodoCustoWhatsappForm(forms.Form):
    """Periodo do relatorio de custo do WhatsApp (datas inclusivas)."""

    # O <input type="date"> so aceita AAAA-MM-DD; o formato local (dd/mm/aaaa) deixaria o campo vazio.
    inicio = forms.DateField(label='De', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))
    fim = forms.DateField(label='Ate', widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'))

    def clean(self):
        from apps.acolhimento.custo_whatsapp import MAX_DIAS_PERIODO

        cleaned = super().clean()
        inicio, fim = cleaned.get('inicio'), cleaned.get('fim')
        if inicio and fim:
            if fim < inicio:
                self.add_error('fim', 'A data final deve ser igual ou posterior a inicial.')
            elif (fim - inicio).days > MAX_DIAS_PERIODO:
                self.add_error('fim', 'Escolha um periodo de ate um ano.')
        return cleaned


class TabelaPrecoWhatsappForm(forms.ModelForm):
    class Meta:
        model = TabelaPrecoWhatsapp
        fields = ['moeda', 'preco_marketing', 'preco_utility', 'preco_authentication', 'preco_service']
        widgets = {
            campo: forms.NumberInput(attrs={'step': '0.0001', 'min': '0'})
            for campo in ['preco_marketing', 'preco_utility', 'preco_authentication', 'preco_service']
        }

    def clean(self):
        cleaned = super().clean()
        for campo in ['preco_marketing', 'preco_utility', 'preco_authentication', 'preco_service']:
            valor = cleaned.get(campo)
            if valor is not None and valor < 0:
                self.add_error(campo, 'O preco nao pode ser negativo.')
        return cleaned
