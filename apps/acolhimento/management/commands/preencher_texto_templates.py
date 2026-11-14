"""Troca o marcador ("Template opt-in enfileirado") pelo texto real do template.

Mensagens enviadas pela API da Meta antes desta mudanca guardavam so um marcador
no conteudo. Aqui o texto e montado de novo a partir do template aprovado e das
variaveis gravadas em `metadata_envio.meta_template` (as mesmas que foram enviadas).

    python manage.py preencher_texto_templates --dry-run
    python manage.py preencher_texto_templates
"""
from django.core.management.base import BaseCommand

from apps.acolhimento import template_config
from apps.acolhimento.models import MensagemContato


class Command(BaseCommand):
    help = 'Grava o texto real dos templates da Meta nas mensagens que mostram so o marcador.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Mostra o que mudaria, sem gravar.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        candidatas = MensagemContato.objects.filter(
            canal=MensagemContato.CanalChoices.WHATSAPP,
            direcao=MensagemContato.DirecaoChoices.SAIDA,
            metadata_envio__has_key='meta_template',
        ).only('id', 'conteudo', 'metadata_envio')

        atualizadas = sem_texto = 0
        for mensagem in candidatas.iterator():
            if not template_config.eh_marcador_template(mensagem.conteudo):
                continue
            texto = template_config.texto_meta_template((mensagem.metadata_envio or {}).get('meta_template'))
            if not texto:
                sem_texto += 1
                continue
            atualizadas += 1
            if dry_run:
                self.stdout.write(f'[{mensagem.id}] {texto[:80]!r}')
            else:
                MensagemContato.objects.filter(pk=mensagem.pk).update(conteudo=texto)

        acao = 'seriam atualizadas' if dry_run else 'atualizadas'
        self.stdout.write(self.style.SUCCESS(f'{atualizadas} mensagem(ns) {acao}.'))
        if sem_texto:
            self.stdout.write(self.style.WARNING(
                f'{sem_texto} mensagem(ns) sem texto: nao foi possivel consultar o template na Meta '
                '(confira META_WA_BUSINESS_ACCOUNT_ID e o token).'
            ))
