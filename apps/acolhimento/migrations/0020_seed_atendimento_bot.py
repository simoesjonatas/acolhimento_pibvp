from django.db import migrations


DEFAULT_SAUDACAO = (
    'Ola! Que bom falar com voce. Sou o atendimento da igreja. '
    'Com o que posso te ajudar hoje?'
)
DEFAULT_FALLBACK = 'Desculpa, nao entendi. Responda com o numero de uma das opcoes abaixo:'

OPCOES_EXEMPLO = [
    {
        'ordem': 1,
        'rotulo': 'Falar com alguem',
        'palavras_chave': 'atendente, falar, pessoa, ajuda, atendimento',
        'resposta': (
            'Perfeito! Ja vou avisar alguem da nossa equipe para te atender. '
            'Aguarde so um pouquinho.'
        ),
        'acao': 'transferir_humano',
    },
    {
        'ordem': 2,
        'rotulo': 'Horarios dos cultos',
        'palavras_chave': 'horario, horarios, culto, cultos, programacao',
        'resposta': 'Nossos cultos: [edite aqui com os dias e horarios da sua igreja].',
        'acao': 'responder',
    },
]


def seed(apps, schema_editor):
    Config = apps.get_model('acolhimento', 'ConfiguracaoAtendimentoBot')
    Opcao = apps.get_model('acolhimento', 'OpcaoAtendimentoBot')

    Config.objects.get_or_create(
        pk=1,
        defaults={
            'ativo': False,
            'mensagem_saudacao': DEFAULT_SAUDACAO,
            'mensagem_fallback': DEFAULT_FALLBACK,
        },
    )
    # So semeia opcoes de exemplo se ainda nao houver nenhuma (nao sobrescreve edicoes).
    if not Opcao.objects.exists():
        for dados in OPCOES_EXEMPLO:
            Opcao.objects.create(**dados)


def unseed(apps, schema_editor):
    # Nao remove dados: podem ter sido editados pelo usuario.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('acolhimento', '0019_opcaoatendimentobot_primeirocontato_bot_etapa_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
