"""Aprende os LIDs do WhatsApp a partir do historico de mensagens da Evolution.

O webhook so descobre o LID de alguem quando essa pessoa escreve. Este comando
recupera de uma vez os LIDs que ja estao no historico da instancia, para nao ficar
esperando cada contato mandar mensagem antes de conseguir responder a ele.

Uso:
    python manage.py sincronizar_lids_whatsapp
    python manage.py sincronizar_lids_whatsapp --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.acolhimento import evolution_service
from apps.acolhimento.models import PrimeiroContato
from apps.acolhimento.phone_utils import find_pessoa_by_phone


class Command(BaseCommand):
    help = 'Sincroniza os LIDs do WhatsApp dos contatos a partir do historico da Evolution.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limite',
            type=int,
            default=500,
            help='Quantas mensagens do historico varrer (padrao: 500).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Mostra o que seria atualizado, sem gravar.',
        )

    def handle(self, *args, **options):
        try:
            mapa = evolution_service.mapear_lids(limite=options['limite'])
        except evolution_service.EvolutionWhatsAppError as exc:
            raise CommandError(str(exc)) from exc

        if not mapa:
            self.stdout.write('Nenhum mapeamento telefone -> LID encontrado no historico.')
            return

        self.stdout.write(f'{len(mapa)} mapeamento(s) telefone -> LID encontrados.')
        atualizados = 0
        for telefone, lid in sorted(mapa.items()):
            pessoa = find_pessoa_by_phone(telefone)
            if not pessoa:
                self.stdout.write(f'  {telefone} -> {lid} (sem pessoa cadastrada, ignorado)')
                continue
            if pessoa.whatsapp_lid == lid:
                continue
            self.stdout.write(f'  {pessoa.nome}: {telefone} -> {lid}')
            if not options['dry_run']:
                PrimeiroContato.objects.filter(pk=pessoa.pk).update(whatsapp_lid=lid)
            atualizados += 1

        if options['dry_run']:
            self.stdout.write(self.style.WARNING(f'DRY RUN: {atualizados} pessoa(s) seriam atualizadas.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'{atualizados} pessoa(s) atualizadas.'))
