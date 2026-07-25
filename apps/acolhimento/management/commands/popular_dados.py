import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.acolhimento.models import (
    CampanhaComunicacao,
    ExecucaoProcessamentoFila,
    InteracaoAcolhimento,
    MensagemContato,
    PrimeiroContato,
)

SEED_TOKEN = '[seed_demo]'
DEMO_USER_PREFIX = 'demo.'

NOMES_FEM = [
    'Ana', 'Beatriz', 'Camila', 'Daniela', 'Eduarda', 'Fernanda', 'Gabriela',
    'Helena', 'Isabela', 'Juliana', 'Larissa', 'Mariana', 'Natalia', 'Patricia',
    'Renata', 'Sofia', 'Tatiane', 'Vanessa', 'Amanda', 'Bruna', 'Carolina', 'Debora',
]
NOMES_MASC = [
    'Andre', 'Bruno', 'Carlos', 'Daniel', 'Eduardo', 'Felipe', 'Gustavo',
    'Henrique', 'Igor', 'Joao', 'Lucas', 'Marcelo', 'Nicolas', 'Otavio',
    'Paulo', 'Rafael', 'Samuel', 'Thiago', 'Vinicius', 'Wesley', 'Rodrigo', 'Diego',
]
SOBRENOMES = [
    'Silva', 'Santos', 'Oliveira', 'Souza', 'Rodrigues', 'Ferreira', 'Alves',
    'Pereira', 'Lima', 'Gomes', 'Costa', 'Ribeiro', 'Martins', 'Carvalho',
    'Almeida', 'Lopes', 'Soares', 'Fernandes', 'Vieira', 'Barbosa', 'Rocha', 'Dias',
]
CIDADES = [
    'Belo Horizonte', 'Contagem', 'Betim', 'Nova Lima', 'Sabara', 'Santa Luzia',
    'Ribeirao das Neves', 'Ibirite', 'Vespasiano', 'Lagoa Santa', 'Sete Lagoas',
    'Uberlandia', 'Juiz de Fora', 'Sao Paulo', 'Rio de Janeiro',
]
RELIGIOES = [
    'Catolica', 'Evangelica', 'Batista', 'Espirita', 'Sem religiao',
    'Assembleia de Deus', 'Presbiteriana', 'Nenhuma', '',
]
DDDS = ['31', '31', '31', '37', '35', '34', '32', '11', '21', '38']

OBS_TEXTOS = [
    'Demonstrou interesse em participar do grupo de jovens.',
    'Passa por um momento dificil na familia, pediu oracao.',
    'Chegou por indicacao de um membro da igreja.',
    'Prefere contato no periodo da noite.',
    'Ja frequentou outra igreja na cidade.',
    'Tem interesse em conhecer o grupo de louvor.',
    'Solicitou visita da equipe de acolhimento.',
    'Primeira visita foi muito acolhedora, quer voltar.',
    '',
    '',
]

MSG_SAIDA = [
    'Ola! Aqui e a equipe de acolhimento da PIBVP. Que alegria ter voce com a gente! Como podemos te ajudar?',
    'Oi! Passando para lembrar do nosso encontro neste domingo as 18h. Vai ser especial ter voce aqui!',
    'Bom dia! Estamos orando por voce. Se precisar conversar, estamos a disposicao.',
    'Ola! Gostariamos de te convidar para o nosso grupo de acolhimento. Tem interesse em participar?',
    'Oi! Como voce esta? Faz um tempinho que nao conversamos, queria saber como estao as coisas.',
    'Ola! Segue o endereco da igreja para o culto de domingo. Sera um prazer te receber!',
]
MSG_ENTRADA = [
    'Oi! Muito obrigado pelo contato, fico feliz demais!',
    'Bom dia! Sim, tenho interesse em participar sim.',
    'Ola, obrigado pela oracao. Estou precisando muito.',
    'Vou tentar ir no domingo sim, obrigado pelo convite!',
    'Oi, pode ser no fim da tarde? De manha trabalho.',
    'Que legal! Como faco para participar do grupo?',
    'Obrigado, estou bem melhor essa semana. Deus abencoe!',
]

# Conversa longa (50 mensagens) alternando equipe (S) e pessoa (E).
DIALOGO = [
    ('S', 'Ola, Beatriz! Aqui e a equipe de acolhimento da PIBVP. Que bom ter voce com a gente no culto de domingo! :)'),
    ('E', 'Oii! Muito obrigada, gostei demais de estar ai!'),
    ('S', 'Ficamos felizes! Como voce ficou sabendo da nossa igreja?'),
    ('E', 'Foi uma amiga do trabalho que me convidou, a Carla.'),
    ('S', 'Que bencao! A Carla e uma pessoa incrivel. E a sua primeira vez numa igreja?'),
    ('E', 'Nao, ja frequentei quando era mais nova, mas fazia tempo que nao ia.'),
    ('S', 'Entendo. E o que te motivou a voltar agora, se puder compartilhar?'),
    ('E', 'Passei por uns momentos dificeis esse ano e senti falta de me reconectar com Deus.'),
    ('S', 'Sentimos muito pelo momento dificil, mas que alegria saber que voce esta buscando essa reconexao. Estamos aqui com voce.'),
    ('E', 'Obrigada mesmo, isso significa muito pra mim.'),
    ('S', 'Voce gostaria de participar de um dos nossos grupos pequenos durante a semana? Sao encontros bem acolhedores.'),
    ('E', 'Gostaria sim! Como funciona?'),
    ('S', 'Sao grupos de umas 10 pessoas que se reunem numa casa pra conversar, orar e estudar a Biblia. Qual regiao voce mora?'),
    ('E', 'Moro no bairro Santa Efigenia.'),
    ('S', 'Perfeito! Tem um grupo que se reune as quartas as 20h pertinho de voce. Posso te passar o contato da lider?'),
    ('E', 'Pode sim, por favor!'),
    ('S', 'A lider e a Fernanda, super querida. Voce prefere ser chamada de Beatriz ou tem algum apelido?'),
    ('E', 'Pode ser Bia mesmo :)'),
    ('S', 'Combinado, Bia! E me conta, voce tem interesse em algum ministerio? Louvor, criancas, acolhimento...'),
    ('E', 'Ah eu adoro cantar! Sempre tive vontade de participar de um coral ou louvor.'),
    ('S', 'Que maravilha! Temos um ministerio de louvor lindo. Posso te apresentar pro pastor de musica?'),
    ('E', 'Nossa, adoraria! Mas sera que eu teria nivel pra isso? Faz tempo que nao canto.'),
    ('S', 'Nao se preocupe com isso, o importante e o coracao. Tem gente de todos os niveis e todos sao bem-vindos.'),
    ('E', 'Isso me deixa mais tranquila haha. Obrigada pelo carinho de voces.'),
    ('S', 'Vai ter um cafe de boas-vindas domingo que vem, as 9h, antes do culto. Voce consegue vir?'),
    ('E', 'Consigo sim! Vou adorar conhecer mais gente.'),
    ('S', 'Que otimo! Vou colocar seu nome na lista. Vai ter um cantinho especial pros novos, voce nao vai ficar perdida.'),
    ('E', 'Ahh que cuidado, obrigada! Posso levar minha filha? Ela tem 8 anos.'),
    ('S', 'Claro que pode! Temos o ministerio infantil no mesmo horario, com atividades bem legais. Qual o nome dela?'),
    ('E', 'O nome dela e Alice.'),
    ('S', 'Que nome lindo! A Alice vai amar. As tias do infantil sao um amor.'),
    ('E', 'Que alivio saber que ela vai ter um espaco so dela tambem.'),
    ('S', 'Com certeza! E se voce precisar de qualquer coisa durante a semana, e so me chamar aqui, ta?'),
    ('E', 'Ta bom! Voces tem algum estudo pra quem ta recomecando na fe?'),
    ('S', 'Temos sim! Um curso chamado Primeiros Passos, perfeito pra quem esta recomecando. Comeca turma nova mes que vem.'),
    ('E', 'Quero muito participar desse!'),
    ('S', 'Anotado! Vou te avisar quando abrir as inscricoes. Voce prefere material impresso ou digital?'),
    ('E', 'Pode ser digital, e mais pratico pra mim.'),
    ('S', 'Perfeito. Bia, como voce esta se sentindo com tudo isso? Nao quero te sobrecarregar de informacao haha'),
    ('E', 'Que nada, to me sentindo muito acolhida! Faz tempo que nao me sentia parte de algo assim.'),
    ('S', 'Ler isso enche nosso coracao. E exatamente pra isso que estamos aqui.'),
    ('E', 'De verdade, obrigada. Voces chegaram num momento que eu precisava muito.'),
    ('S', 'Deus tem o tempo perfeito. Estamos orando por voce e pela Alice essa semana. Tem algum pedido especifico?'),
    ('E', 'Pecam pela minha mae, que esta doente. E pela minha nova fase no trabalho.'),
    ('S', 'Vamos levar sua mae e seu trabalho em oracao com todo carinho. Como e o nome da sua mae?'),
    ('E', 'O nome dela e Dona Marlene.'),
    ('S', 'Vamos orar pela Dona Marlene e pela recuperacao dela. Qualquer novidade, me conta, ta?'),
    ('E', 'Pode deixar! Muito obrigada por tudo, viu? Voces sao especiais.'),
    ('S', 'Nos que agradecemos, Bia. Seja muito bem-vinda a familia PIBVP! Ate domingo no cafe.'),
    ('E', 'Ate domingo! Ja to ansiosa :)'),
]


class Command(BaseCommand):
    help = 'Popula o banco com dados de demonstracao (pessoas, mensagens, interacoes, campanhas). Use --limpar para remover.'

    def add_arguments(self, parser):
        parser.add_argument('--pessoas', type=int, default=60, help='Quantidade de pessoas a criar (padrao: 60).')
        parser.add_argument('--conversa', action='store_true', help='Cria apenas uma pessoa com uma conversa longa (50 mensagens) no WhatsApp.')
        parser.add_argument('--limpar', action='store_true', help='Remove todos os dados de demonstracao criados por este comando.')
        parser.add_argument('--seed', type=int, default=42, help='Semente do gerador aleatorio para resultados reproduziveis.')

    def handle(self, *args, **options):
        if options['limpar']:
            self._limpar()
            return

        random.seed(options['seed'])

        if options['conversa']:
            with transaction.atomic():
                pessoa, total_msgs = self._criar_conversa()
            self.stdout.write(self.style.SUCCESS(
                f'Conversa criada: {pessoa.nome} com {total_msgs} mensagens. '
                f'Veja em /acolhimento/pessoas/{pessoa.pk}/mensagens/'
            ))
            return

        total = max(int(options['pessoas']), 1)

        with transaction.atomic():
            voluntarios = self._criar_voluntarios()
            staff = get_user_model().objects.filter(is_staff=True).first()
            responsaveis_pool = [u for u in [staff, *voluntarios] if u is not None]

            pessoas = self._criar_pessoas(total, responsaveis_pool, staff)
            self._criar_mensagens(pessoas, staff)
            self._criar_interacoes(pessoas)
            self._criar_campanhas(pessoas, staff)
            self._criar_execucoes(staff)

        self.stdout.write(self.style.SUCCESS(
            f'Dados de demonstracao criados: {len(pessoas)} pessoas '
            f'(marcadas com "{SEED_TOKEN}"). Use "--limpar" para remover.'
        ))

    # ------------------------------------------------------------------ utils

    def _agora_menos(self, dias=0, horas=0):
        return timezone.now() - timedelta(days=dias, hours=horas, minutes=random.randint(0, 59))

    def _telefone(self, indice, origem):
        ddd = DDDS[indice % len(DDDS)]
        local = f'9{60000000 + indice:08d}'  # 9 + 8 digitos = celular
        if origem == PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO:
            return f'{ddd}{local}'  # webhook grava so digitos
        return f'({ddd}) {local[:5]}-{local[5:]}'  # equipe grava com mascara

    # --------------------------------------------------------------- criacao

    def _criar_voluntarios(self):
        User = get_user_model()
        base = [
            ('demo.ana', 'Ana', 'Ribeiro'),
            ('demo.marcos', 'Marcos', 'Andrade'),
            ('demo.juliana', 'Juliana', 'Prado'),
            ('demo.rafael', 'Rafael', 'Nunes'),
        ]
        criados = []
        for username, first, last in base:
            user, novo = User.objects.get_or_create(
                username=username,
                defaults={
                    'first_name': first,
                    'last_name': last,
                    'email': f'{username}@exemplo.com.br',
                    'is_staff': True,
                    'is_active': True,
                },
            )
            if novo:
                user.set_unusable_password()
                user.save(update_fields=['password'])
            criados.append(user)
        return criados

    def _criar_pessoas(self, total, responsaveis_pool, staff):
        offset = PrimeiroContato.objects.filter(observacoes__contains=SEED_TOKEN).count()
        status_opcoes = [c[0] for c in PrimeiroContato.StatusAcolhimento.choices]
        # ordem: primeiro_contato, robo, em_acompanhamento, participante, membro
        status_pesos = [38, 20, 20, 14, 8]  # mais no inicio do funil, menos membros
        como_opcoes = [c[0] for c in PrimeiroContato.ComoConheceuChoices.choices]
        busca_opcoes = [c[0] for c in PrimeiroContato.OQueBuscaChoices.choices]
        civil_opcoes = [c[0] for c in PrimeiroContato.EstadoCivilChoices.choices] + ['']

        pessoas = []
        for i in range(total):
            indice = offset + i
            if random.random() < 0.55:
                genero = PrimeiroContato.GeneroChoices.FEMININO
                primeiro = random.choice(NOMES_FEM)
            else:
                genero = PrimeiroContato.GeneroChoices.MASCULINO
                primeiro = random.choice(NOMES_MASC)
            nome = f'{primeiro} {random.choice(SOBRENOMES)}'

            origem = random.choices(
                [PrimeiroContato.OrigemCadastroChoices.EQUIPE, PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO],
                weights=[70, 30],
            )[0]

            obs = random.choice(OBS_TEXTOS)
            observacoes = f'{obs}\n{SEED_TOKEN}'.strip() if obs else SEED_TOKEN

            pessoa = PrimeiroContato.objects.create(
                nome=nome,
                telefone_whatsapp=self._telefone(indice, origem),
                primeira_vez=random.random() < 0.6,
                como_conheceu=random.choice(como_opcoes),
                o_que_busca=random.choice(busca_opcoes),
                origem_cadastro=origem,
                iniciou_interacao=origem == PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO and random.random() < 0.7,
                criado_por=None if origem == PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO else staff,
                responsavel_atual=random.choice(responsaveis_pool) if (responsaveis_pool and random.random() < 0.75) else None,
                email=f'{primeiro.lower()}.{i}@exemplo.com.br' if random.random() < 0.6 else '',
                genero=genero,
                idade=random.randint(16, 68) if random.random() < 0.8 else None,
                religiao=random.choice(RELIGIOES),
                estado_civil=random.choice(civil_opcoes),
                cidade=random.choice(CIDADES) if random.random() < 0.85 else '',
                observacoes=observacoes,
                status=random.choices(status_opcoes, weights=status_pesos)[0],
            )

            # espalha as datas de cadastro ao longo dos ultimos ~120 dias
            criado = self._agora_menos(dias=random.randint(0, 120))
            PrimeiroContato.objects.filter(pk=pessoa.pk).update(
                criado_em=criado,
                data_primeiro_contato=criado.date(),
                atualizado_em=criado,
            )
            pessoa.criado_em = criado
            pessoas.append(pessoa)

        return pessoas

    def _criar_mensagens(self, pessoas, staff):
        status_saida = [
            MensagemContato.StatusFilaChoices.ENVIADA,
            MensagemContato.StatusFilaChoices.ENVIADA,
            MensagemContato.StatusFilaChoices.PENDENTE,
            MensagemContato.StatusFilaChoices.FALHA,
            MensagemContato.StatusFilaChoices.CANCELADA,
        ]

        for pessoa in pessoas:
            base_dt = getattr(pessoa, 'criado_em', timezone.now())

            # mensagens de saida
            for _ in range(random.randint(0, 4)):
                status = random.choice(status_saida)
                enfileirada = base_dt + timedelta(days=random.randint(0, 20), hours=random.randint(0, 20))
                if enfileirada > timezone.now():
                    enfileirada = self._agora_menos(dias=random.randint(0, 5))

                enviada_em = None
                entregue_em = None
                lida_em = None
                erro = ''
                if status == MensagemContato.StatusFilaChoices.ENVIADA:
                    enviada_em = enfileirada + timedelta(minutes=random.randint(1, 30))
                    entregue_em = enviada_em + timedelta(minutes=random.randint(1, 20))
                    if random.random() < 0.6:
                        lida_em = entregue_em + timedelta(minutes=random.randint(1, 120))
                elif status == MensagemContato.StatusFilaChoices.FALHA:
                    erro = '63016 | Falha no envio do template | failed'

                resposta_em = None
                resposta_txt = ''
                if status == MensagemContato.StatusFilaChoices.ENVIADA and random.random() < 0.4:
                    resposta_em = (lida_em or entregue_em) + timedelta(hours=random.randint(1, 48))
                    resposta_txt = random.choice(MSG_ENTRADA)

                msg = MensagemContato.objects.create(
                    pessoa=pessoa,
                    criado_por=pessoa.responsavel_atual or staff,
                    canal=MensagemContato.CanalChoices.WHATSAPP,
                    direcao=MensagemContato.DirecaoChoices.SAIDA,
                    status_fila=status,
                    prioridade=random.randint(3, 7),
                    conteudo=random.choice(MSG_SAIDA),
                    tentativas_envio=1 if status in (MensagemContato.StatusFilaChoices.ENVIADA, MensagemContato.StatusFilaChoices.FALHA) else 0,
                    erro_ultimo_envio=erro,
                    enviada_em=enviada_em,
                    entregue_em=entregue_em,
                    lida_em=lida_em,
                    resposta_recebida_em=resposta_em,
                    resposta_conteudo=resposta_txt,
                )
                MensagemContato.objects.filter(pk=msg.pk).update(enfileirada_em=enfileirada, atualizado_em=enfileirada)

            # mensagens de entrada (retornos), algumas ainda nao vistas pela equipe
            if random.random() < 0.4:
                for _ in range(random.randint(1, 2)):
                    recebida = self._agora_menos(dias=random.randint(0, 15))
                    vista = None if random.random() < 0.5 else recebida + timedelta(hours=random.randint(1, 24))
                    msg = MensagemContato.objects.create(
                        pessoa=pessoa,
                        canal=MensagemContato.CanalChoices.WHATSAPP,
                        direcao=MensagemContato.DirecaoChoices.ENTRADA,
                        status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
                        conteudo=random.choice(MSG_ENTRADA),
                        enviada_em=recebida,
                        entregue_em=recebida,
                        visualizada_equipe_em=vista,
                    )
                    MensagemContato.objects.filter(pk=msg.pk).update(enfileirada_em=recebida, atualizado_em=recebida)
                if not pessoa.iniciou_interacao:
                    PrimeiroContato.objects.filter(pk=pessoa.pk).update(iniciou_interacao=True)

    def _criar_interacoes(self, pessoas):
        tipos = [c[0] for c in InteracaoAcolhimento.TipoInteracao.choices]
        descricoes = {
            InteracaoAcolhimento.TipoInteracao.TENTATIVA_CONTATO: 'Tentativa de contato por WhatsApp, sem resposta ate o momento.',
            InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA: 'Respondeu a mensagem e demonstrou interesse.',
            InteracaoAcolhimento.TipoInteracao.VISITA_AGENDADA: 'Visita agendada para a proxima semana.',
            InteracaoAcolhimento.TipoInteracao.VISITA_REALIZADA: 'Visita realizada, foi muito bem recebido pela equipe.',
            InteracaoAcolhimento.TipoInteracao.OBSERVACAO: 'Observacao geral registrada pela equipe de acolhimento.',
        }
        for pessoa in pessoas:
            if random.random() < 0.55:
                for _ in range(random.randint(1, 4)):
                    tipo = random.choice(tipos)
                    InteracaoAcolhimento.objects.create(
                        pessoa=pessoa,
                        tipo=tipo,
                        descricao=descricoes.get(tipo, ''),
                        data_interacao=self._agora_menos(dias=random.randint(0, 60)).date(),
                    )

    def _criar_campanhas(self, pessoas, staff):
        definicoes = [
            ('Boas-vindas novos contatos', CampanhaComunicacao.StatusCampanhaChoices.CONCLUIDA, -30),
            ('Convite culto de domingo', CampanhaComunicacao.StatusCampanhaChoices.EM_EXECUCAO, 0),
            ('Reengajamento sem resposta', CampanhaComunicacao.StatusCampanhaChoices.AGENDADA, 5),
            ('Campanha de oracao', CampanhaComunicacao.StatusCampanhaChoices.RASCUNHO, None),
        ]
        for titulo, status, dias in definicoes:
            agendada = None
            if dias is not None:
                agendada = timezone.now() + timedelta(days=dias)
            campanha = CampanhaComunicacao.objects.create(
                titulo=titulo,
                descricao=f'Campanha de demonstracao. {SEED_TOKEN}',
                canal=CampanhaComunicacao.CanalChoices.WHATSAPP,
                publico_alvo_descricao='Contatos recentes da fila de acolhimento.',
                agendada_para=agendada,
                status=status,
                criado_por=staff,
            )
            alvo = random.sample(pessoas, min(len(pessoas), random.randint(8, 20)))
            campanha.contatos_alvo.set(alvo)

    def _criar_execucoes(self, staff):
        for i in range(2):
            iniciado = self._agora_menos(dias=random.randint(1, 20))
            total = random.randint(10, 20)
            sucesso = random.randint(int(total * 0.6), total)
            execucao = ExecucaoProcessamentoFila.objects.create(
                solicitado_por=staff,
                status=ExecucaoProcessamentoFila.StatusExecucaoChoices.CONCLUIDA,
                limite=20,
                dry_run=False,
                total_selecionado=total,
                total_processado=total,
                total_sucesso=sucesso,
                total_falha=total - sucesso,
                log_execucao=f'[{iniciado:%d/%m/%Y %H:%M}] Processamento concluido. {SEED_TOKEN}',
                finalizado_em=iniciado + timedelta(minutes=random.randint(1, 10)),
            )
            ExecucaoProcessamentoFila.objects.filter(pk=execucao.pk).update(iniciado_em=iniciado)

    def _criar_conversa(self):
        from apps.acolhimento.phone_utils import find_pessoa_by_phone

        staff = get_user_model().objects.filter(is_staff=True).first()

        # garante um telefone unico (respeitando a regra de WhatsApp duplicado)
        indice = 900
        while True:
            telefone = self._telefone(indice, PrimeiroContato.OrigemCadastroChoices.EQUIPE)
            if find_pessoa_by_phone(telefone) is None:
                break
            indice += 1

        inicio = timezone.now() - timedelta(days=14)
        pessoa = PrimeiroContato.objects.create(
            nome='Beatriz Almeida',
            telefone_whatsapp=telefone,
            primeira_vez=False,
            como_conheceu=PrimeiroContato.ComoConheceuChoices.INDICACAO,
            o_que_busca=PrimeiroContato.OQueBuscaChoices.RESTAURAR_VIDA,
            origem_cadastro=PrimeiroContato.OrigemCadastroChoices.EQUIPE,
            iniciou_interacao=True,
            criado_por=staff,
            responsavel_atual=staff,
            email='beatriz.almeida@exemplo.com.br',
            genero=PrimeiroContato.GeneroChoices.FEMININO,
            idade=34,
            religiao='Evangelica',
            estado_civil=PrimeiroContato.EstadoCivilChoices.DIVORCIADO,
            cidade='Belo Horizonte',
            observacoes=f'Retornou a igreja apos periodo dificil. Tem uma filha (Alice, 8 anos).\n{SEED_TOKEN}',
            status=PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO,
        )
        PrimeiroContato.objects.filter(pk=pessoa.pk).update(
            criado_em=inicio, data_primeiro_contato=inicio.date(), atualizado_em=inicio,
        )

        total = len(DIALOGO)
        ts = inicio
        prev_saida = None
        for idx, (direcao, texto) in enumerate(DIALOGO):
            ts = ts + timedelta(minutes=random.randint(3, 90))
            if random.random() < 0.25:
                ts = ts + timedelta(hours=random.randint(2, 20))  # de vez em quando um intervalo maior
            if ts >= timezone.now():
                ts = timezone.now() - timedelta(minutes=(total - idx))
            ultima = idx == total - 1

            if direcao == 'S':
                msg = MensagemContato.objects.create(
                    pessoa=pessoa,
                    criado_por=staff,
                    canal=MensagemContato.CanalChoices.WHATSAPP,
                    direcao=MensagemContato.DirecaoChoices.SAIDA,
                    status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
                    prioridade=5,
                    conteudo=texto,
                    tentativas_envio=1,
                    enviada_em=ts,
                    entregue_em=ts + timedelta(minutes=1),
                    lida_em=ts + timedelta(minutes=random.randint(2, 15)),
                )
                prev_saida = msg
            else:
                vista = None if ultima else ts + timedelta(minutes=random.randint(2, 30))
                msg = MensagemContato.objects.create(
                    pessoa=pessoa,
                    canal=MensagemContato.CanalChoices.WHATSAPP,
                    direcao=MensagemContato.DirecaoChoices.ENTRADA,
                    status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
                    conteudo=texto,
                    enviada_em=ts,
                    entregue_em=ts,
                    visualizada_equipe_em=vista,
                )
                if prev_saida is not None:
                    MensagemContato.objects.filter(pk=prev_saida.pk).update(
                        resposta_recebida_em=ts, resposta_conteudo=texto,
                    )
                    prev_saida = None

            MensagemContato.objects.filter(pk=msg.pk).update(enfileirada_em=ts, atualizado_em=ts)

        InteracaoAcolhimento.objects.create(
            pessoa=pessoa,
            tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
            descricao='Respondeu no WhatsApp e demonstrou muito interesse. Conversa em andamento.',
            data_interacao=(inicio + timedelta(days=1)).date(),
        )
        InteracaoAcolhimento.objects.create(
            pessoa=pessoa,
            tipo=InteracaoAcolhimento.TipoInteracao.VISITA_AGENDADA,
            descricao='Convidada para o cafe de boas-vindas antes do culto de domingo.',
            data_interacao=(inicio + timedelta(days=7)).date(),
        )

        return pessoa, total

    # ---------------------------------------------------------------- limpeza

    def _limpar(self):
        campanhas = CampanhaComunicacao.objects.filter(descricao__contains=SEED_TOKEN)
        execucoes = ExecucaoProcessamentoFila.objects.filter(log_execucao__contains=SEED_TOKEN)
        pessoas = PrimeiroContato.objects.filter(observacoes__contains=SEED_TOKEN)

        n_campanhas = campanhas.count()
        n_execucoes = execucoes.count()
        n_pessoas = pessoas.count()

        campanhas.delete()
        execucoes.delete()
        pessoas.delete()  # cascata remove mensagens e interacoes vinculadas

        usuarios = get_user_model().objects.filter(username__startswith=DEMO_USER_PREFIX)
        n_usuarios = usuarios.count()
        usuarios.delete()

        self.stdout.write(self.style.SUCCESS(
            f'Removidos: {n_pessoas} pessoas, {n_campanhas} campanhas, '
            f'{n_execucoes} execucoes e {n_usuarios} usuarios de demonstracao.'
        ))
