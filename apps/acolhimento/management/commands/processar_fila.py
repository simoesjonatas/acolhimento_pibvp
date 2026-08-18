"""Processa a fila de mensagens de saida — entrypoint para cron/systemd.

Diferente do comando de baixo nivel `processar_fila_mensagens` (que so chama o
processador), este:
  1. recupera execucoes presas (processo que morreu no meio);
  2. cria um registro `ExecucaoProcessamentoFila` (origem=automatico) para o
     historico/observabilidade;
  3. roda de forma SINCRONA no proprio processo do cron (sem threads).

O claim atomico em `fila_processor` garante que, mesmo se este comando rodar junto
com o modo automatico (thread) ou com outro cron, cada mensagem e enviada uma vez.

Uso via cron e OPCIONAL: a fila tambem e drenada pelo modo automatico (thread) ou
manualmente pela tela de processamento. Exemplos (a cada minuto):
    bare-metal:  * * * * * cd /app && /app/.venv/bin/python manage.py processar_fila --limit 100
    docker:      * * * * * cd /projeto && docker compose exec -T web python manage.py processar_fila --limit 100
"""
from django.core.management.base import BaseCommand

from apps.acolhimento import fila_auto
from apps.acolhimento.models import ExecucaoProcessamentoFila


class Command(BaseCommand):
    help = 'Processa a fila de mensagens de saida do WhatsApp (uso em cron/systemd).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100, help='Maximo de mensagens por rodada.')
        parser.add_argument('--dry-run', action='store_true', help='Simula sem enviar.')
        parser.add_argument(
            '--force',
            action='store_true',
            help='Roda mesmo se houver uma execucao marcada como ativa.',
        )

    def handle(self, *args, **options):
        limit = max(int(options['limit']), 1)
        dry_run = bool(options.get('dry_run'))
        force = bool(options.get('force'))

        recuperadas = fila_auto.recuperar_execucoes_presas()
        if recuperadas:
            self.stdout.write(self.style.WARNING(f'{recuperadas} execucao(oes) presa(s) recuperada(s).'))

        if not force and fila_auto.ha_execucao_ativa():
            self.stdout.write('Ja existe uma execucao ativa; nada a fazer.')
            return

        if not dry_run and not fila_auto.ha_pendentes_saida():
            self.stdout.write('Nenhuma mensagem pendente na fila.')
            return

        execucao = ExecucaoProcessamentoFila.objects.create(
            status=ExecucaoProcessamentoFila.StatusExecucaoChoices.EXECUTANDO,
            limite=limit,
            dry_run=dry_run,
            origem=ExecucaoProcessamentoFila.OrigemChoices.AUTOMATICO,
        )
        self.stdout.write(f'Execucao #{execucao.id} iniciada (limite={limit}, dry_run={dry_run}).')

        # encadear_auto=False: roda sincrono e nao dispara thread (o proximo tick
        # do cron drena o que sobrar).
        fila_auto.executar_fila_execucao(execucao.id, encadear_auto=False)

        execucao.refresh_from_db()
        resumo = (
            f'Execucao #{execucao.id} {execucao.get_status_display()}: '
            f'sucesso={execucao.total_sucesso} falha={execucao.total_falha} '
            f'processado={execucao.total_processado}/{execucao.total_selecionado}'
        )
        style = self.style.SUCCESS if execucao.total_falha == 0 else self.style.WARNING
        self.stdout.write(style(resumo))
