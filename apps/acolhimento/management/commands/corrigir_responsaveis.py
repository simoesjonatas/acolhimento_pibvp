"""Tira as pessoas de responsaveis que nao atendem mensagens e passa para o rodizio.

Antes, quem cadastrava virava o responsavel mesmo sem o perfil de mensagens, e a
pessoa ficava presa com alguem que nao consegue responder. So mexe em quem ainda
esta em acolhimento (Primeiro contato ate Aprovado).

Sem --aplicar so mostra o que faria. Com --aplicar, cada pessoa vai para o proximo
do rodizio (sem aviso no WhatsApp, como no "Distribuir"); com o rodizio vazio, fica
sem responsavel para um administrador encaminhar.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.acolhimento import atencao, rodizio
from apps.acolhimento.models import InteracaoAcolhimento, PrimeiroContato


class Command(BaseCommand):
    help = 'Passa para o rodizio as pessoas cujo responsavel nao atende mensagens.'

    def add_arguments(self, parser):
        parser.add_argument('--aplicar', action='store_true', help='Faz a troca (sem isso, so mostra).')

    def handle(self, *args, **opcoes):
        sem_permissao = [
            usuario for usuario in get_user_model().objects.filter(primeiros_contatos_responsaveis__isnull=False).distinct()
            if not rodizio.atende_mensagens(usuario)
        ]
        pessoas = list(
            PrimeiroContato.objects.filter(
                responsavel_atual__in=sem_permissao, status__in=atencao.EM_ACOLHIMENTO,
            ).select_related('responsavel_atual').order_by('pk')
        )
        self.stdout.write(f'{len(sem_permissao)} responsável(is) sem perfil de mensagens, {len(pessoas)} pessoa(s) presas.')
        if not opcoes['aplicar']:
            for pessoa in pessoas:
                self.stdout.write(f'  pessoa {pessoa.pk} ({pessoa.get_status_display()}) com usuário {pessoa.responsavel_atual_id}')
            self.stdout.write('Nada alterado. Rode com --aplicar para trocar.')
            return

        destino = {}
        for pessoa in pessoas:
            anterior = pessoa.responsavel_atual
            nome_anterior = anterior.get_full_name() or anterior.get_username()
            with transaction.atomic():
                PrimeiroContato.objects.filter(pk=pessoa.pk).update(responsavel_atual=None)
                pessoa.responsavel_atual = None
                InteracaoAcolhimento.objects.create(
                    pessoa=pessoa,
                    tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
                    descricao=f'{nome_anterior} deixou de ser responsável: não tem o perfil de mensagens.',
                )
                novo = rodizio.atribuir(pessoa, f'era de {nome_anterior}, que não atende mensagens', avisar=False)
            chave = (novo.get_full_name() or novo.get_username()) if novo else 'sem responsável'
            destino[chave] = destino.get(chave, 0) + 1
        resumo = ', '.join(f'{nome}: {quantas}' for nome, quantas in sorted(destino.items()))
        self.stdout.write(self.style.SUCCESS(f'{len(pessoas)} pessoa(s) encaminhada(s). {resumo}'))
