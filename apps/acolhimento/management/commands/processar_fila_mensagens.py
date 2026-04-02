from django.core.management.base import BaseCommand
from apps.acolhimento.fila_processor import processar_fila_mensagens


class Command(BaseCommand):
    help = 'Processa mensagens pendentes de saida via WhatsApp (Twilio).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=20, help='Quantidade maxima de mensagens a processar.')
        parser.add_argument('--ids', nargs='*', type=int, help='Lista opcional de IDs de mensagens para processar.')
        parser.add_argument('--dry-run', action='store_true', help='Nao envia para Twilio; apenas mostra o que seria processado.')

    def handle(self, *args, **options):
        limit = max(int(options['limit']), 0)
        ids = options.get('ids') or []
        dry_run = bool(options.get('dry_run'))

        resultado = processar_fila_mensagens(
            limit=limit,
            ids=ids,
            dry_run=dry_run,
            progress_callback=lambda line: self.stdout.write(line),
        )

        if resultado['total_selecionado'] == 0:
            self.stdout.write(self.style.WARNING('Nenhuma mensagem pendente encontrada para processamento.'))
            return

        resumo = resultado['resumo']
        self.stdout.write(self.style.SUCCESS(resumo) if resultado['falha'] == 0 else self.style.WARNING(resumo))
