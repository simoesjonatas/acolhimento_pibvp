from django.core.management.base import BaseCommand

from apps.acolhimento.models import OpcaoPergunta, PerguntaQuestionario, Questionario

TITULO = 'Formulario de Membresia'

# (texto, tipo, obrigatoria, ajuda, [opcoes])
PERGUNTAS = [
    ('Nome', 'texto_curto', True, '', []),
    ('Sexo', 'escolha_unica', False, '', ['Masculino', 'Feminino']),
    ('Data de nascimento', 'data', True, '', []),
    (
        'Estado civil', 'escolha_unica', False, '',
        ['Solteiro', 'Casado', 'Viuvo', 'Divorciado', 'Desquitado', 'Em uniao estavel', 'Vive com o companheiro(a)'],
    ),
    (
        'Tipo de solicitacao', 'escolha_unica', True, '',
        [
            'Comunicar sua Conversao',
            'Deseja pedir Batismo',
            'Quer ser membro (vindo de outra igreja Batista)',
            'Quer ser membro (vindo de outra Denominacao)',
            'Quer se reconciliar com Deus e com Sua Igreja',
        ],
    ),
    ('Endereco', 'texto_longo', True, '', []),
    ('Data de casamento (se for o caso)', 'data', False, '', []),
    ('Data de conversao', 'data', False, '', []),
    ('Data de batismo', 'data', False, '', []),
    ('Tipo de batismo', 'escolha_unica', False, '', ['Imersao', 'Aspersao / Afusao']),
    ('Igreja onde se batizou', 'texto_curto', False, '', []),
    ('Igreja onde e membro atualmente ou ultima igreja que foi membro', 'texto_curto', False, '', []),
    ('Em quais ministerios/departamentos/organizacoes voce ja trabalhou anteriormente?', 'texto_longo', False, '', []),
    ('Observacao e pedidos de oracao', 'texto_longo', False, '', []),
]


class Command(BaseCommand):
    help = 'Cria o questionario de membresia inicial (baseado no formulario da igreja).'

    def handle(self, *args, **options):
        if Questionario.objects.filter(titulo=TITULO).exists():
            self.stdout.write(self.style.WARNING(f'Questionario "{TITULO}" ja existe. Nada a fazer.'))
            return

        questionario = Questionario.objects.create(
            titulo=TITULO,
            descricao='Formulario de recepcao de novos membros.',
            ativo=True,
            padrao_membresia=True,
        )
        for ordem, (texto, tipo, obrigatoria, ajuda, opcoes) in enumerate(PERGUNTAS):
            pergunta = PerguntaQuestionario.objects.create(
                questionario=questionario,
                texto=texto,
                tipo=tipo,
                obrigatoria=obrigatoria,
                ajuda=ajuda,
                ordem=ordem,
            )
            OpcaoPergunta.objects.bulk_create([
                OpcaoPergunta(pergunta=pergunta, texto=opcao, ordem=indice)
                for indice, opcao in enumerate(opcoes)
            ])

        self.stdout.write(self.style.SUCCESS(
            f'Questionario "{TITULO}" criado com {len(PERGUNTAS)} perguntas.'
        ))
