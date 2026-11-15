"""Baixa o modelo do Whisper local (reserva da transcricao) e, opcionalmente, testa.

Rodar uma vez apos o deploy (os pesos ficam no volume `whisper_modelos`):

    python manage.py preparar_whisper
    python manage.py preparar_whisper --testar /caminho/audio.ogg
"""
import time

from django.core.management.base import BaseCommand, CommandError

from apps.acolhimento import transcricao_local


class Command(BaseCommand):
    help = 'Baixa o modelo do faster-whisper (reserva quando a Groq devolve 429) e testa um audio.'

    def add_arguments(self, parser):
        parser.add_argument('--testar', metavar='ARQUIVO', help='Transcreve este arquivo de audio como teste.')

    def handle(self, *args, **options):
        if not transcricao_local.ativa():
            raise CommandError('TRANSCRICAO_LOCAL_ATIVA esta desligada.')
        inicio = time.monotonic()
        try:
            transcricao_local.carregar_modelo()
        except transcricao_local.TranscricaoLocalIndisponivel as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f'Modelo "{transcricao_local._modelo_nome}" pronto em {time.monotonic() - inicio:.1f}s.'
        ))

        arquivo = options.get('testar')
        if arquivo:
            inicio = time.monotonic()
            resultado = transcricao_local.transcrever(arquivo)
            self.stdout.write(
                f'Audio de {resultado["duracao"]:.1f}s transcrito em {time.monotonic() - inicio:.1f}s:\n'
                f'{resultado["texto"]}'
            )
