"""Entrega da midia recebida pelo WhatsApp (audio, imagem, video, documento).

Os arquivos ficam fora das pastas publicas (`MIDIA_PRIVADA_ROOT`) e so saem por aqui,
para quem pode ver a conversa da pessoa (LGPD). Suporta `Range` porque o Safari
(iPhone) nao toca audio/video sem respostas 206.
"""
from __future__ import annotations

import re

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.acolhimento.models import MensagemContato, OpcaoAtendimentoBot
from apps.acolhimento.views import ConversasPessoasPermissaoMixin

# Tipos que podem abrir no navegador. O resto (inclusive SVG/HTML, que executariam
# script) sai sempre como download.
_PREFIXOS_INLINE = ('audio/', 'video/')
_IMAGENS_INLINE = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
_MIMES_PERIGOSOS = ('html', 'xml', 'svg', 'javascript')
_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')
_BLOCO = 64 * 1024


def _ler_intervalo(arquivo, inicio: int, tamanho: int):
    arquivo.seek(inicio)
    restante = tamanho
    try:
        while restante > 0:
            pedaco = arquivo.read(min(_BLOCO, restante))
            if not pedaco:
                break
            restante -= len(pedaco)
            yield pedaco
    finally:
        arquivo.close()


class MidiaMensagemView(LoginRequiredMixin, ConversasPessoasPermissaoMixin, View):
    def get(self, request, pk, *args, **kwargs):
        mensagem = get_object_or_404(
            MensagemContato,
            pk=pk,
            midia_status=MensagemContato.MidiaStatusChoices.BAIXADA,
        )
        nome = mensagem.midia_nome_original or f'midia-{mensagem.pk}{_sufixo(mensagem.midia_arquivo.name)}'
        return servir_arquivo(request, mensagem.midia_arquivo, mensagem.midia_mime, nome)


class MidiaOpcaoBotView(LoginRequiredMixin, ConversasPessoasPermissaoMixin, View):
    """Arquivo anexado a uma opcao do bot (aparece na conversa de quem o recebeu)."""

    def get(self, request, pk, *args, **kwargs):
        opcao = get_object_or_404(OpcaoAtendimentoBot, pk=pk)
        nome = opcao.midia_nome_original or f'arquivo-{opcao.pk}{_sufixo(opcao.midia_arquivo.name)}'
        return servir_arquivo(request, opcao.midia_arquivo, opcao.midia_mime, nome)


def servir_arquivo(request, campo_arquivo, mime, nome):
    """Entrega um arquivo do storage privado (com Range para audio/video)."""
    if not campo_arquivo:
        raise Http404('Midia indisponivel.')
    try:
        arquivo = campo_arquivo.open('rb')
        tamanho = campo_arquivo.size
    except (FileNotFoundError, OSError) as exc:
        raise Http404('Midia indisponivel.') from exc
    return _MidiaResposta.montar(request, arquivo, tamanho, mime, nome)


class _MidiaResposta:
    @classmethod
    def montar(cls, request, arquivo, tamanho, mime, nome):
        mime = (mime or '').split(';')[0].strip().lower() or 'application/octet-stream'
        inline = mime.startswith(_PREFIXOS_INLINE) or mime in _IMAGENS_INLINE
        if any(perigo in mime for perigo in _MIMES_PERIGOSOS):
            mime = 'application/octet-stream'

        intervalo = _RANGE_RE.match(request.headers.get('Range', '').strip())
        if intervalo and inline and tamanho:
            inicio_txt, fim_txt = intervalo.groups()
            if inicio_txt:
                inicio = int(inicio_txt)
                fim = min(int(fim_txt), tamanho - 1) if fim_txt else tamanho - 1
            elif fim_txt:
                inicio = max(tamanho - int(fim_txt), 0)
                fim = tamanho - 1
            else:
                inicio, fim = 0, tamanho - 1
            if inicio > fim or inicio >= tamanho:
                arquivo.close()
                resposta = HttpResponse(status=416)
                resposta['Content-Range'] = f'bytes */{tamanho}'
                return cls._cabecalhos(resposta)
            resposta = StreamingHttpResponse(
                _ler_intervalo(arquivo, inicio, fim - inicio + 1),
                status=206,
                content_type=mime,
            )
            resposta['Content-Length'] = str(fim - inicio + 1)
            resposta['Content-Range'] = f'bytes {inicio}-{fim}/{tamanho}'
            resposta['Content-Disposition'] = 'inline'
            return cls._cabecalhos(resposta)

        resposta = FileResponse(arquivo, content_type=mime, as_attachment=not inline, filename=nome)
        return cls._cabecalhos(resposta)

    @staticmethod
    def _cabecalhos(resposta):
        resposta['Accept-Ranges'] = 'bytes'
        # Dado pessoal: nada de cache compartilhado; e o arquivo nunca roda script.
        resposta['Cache-Control'] = 'private, max-age=3600'
        resposta['Content-Security-Policy'] = "default-src 'none'; sandbox"
        resposta['X-Content-Type-Options'] = 'nosniff'
        return resposta


def _sufixo(caminho: str) -> str:
    return '.' + caminho.rsplit('.', 1)[-1] if '.' in (caminho or '') else ''
