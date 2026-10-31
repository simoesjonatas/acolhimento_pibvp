"""Webhook da WhatsApp Cloud API (API oficial da Meta): entrada + status de entrega.

Equivalente aos webhooks da Twilio (`TwilioInboundWebhookView` / `TwilioStatusWebhookView`)
e do Evolution (`EvolutionWebhookView`), no formato do Graph API:

  GET  -> verificacao do webhook (hub.mode / hub.verify_token / hub.challenge)
  POST -> {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages",
           "value": {"messages": [...], "statuses": [...], "contacts": [...]}}]}]}

Seguranca: todo POST vem assinado com HMAC-SHA256 do App Secret no cabecalho
X-Hub-Signature-256. Sem o App Secret configurado, em producao o webhook e recusado
(este endpoint cria pessoas/mensagens no banco e nao pode ficar aberto).

Referencia: https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payload-examples
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.acolhimento import atendimento_bot, whatsapp_saude
from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato
from apps.acolhimento.phone_utils import (
    build_auto_nome_from_phone,
    find_pessoa_by_phone,
    phone_for_cadastro,
)

logger = logging.getLogger(__name__)

# Status da Meta -> marco interno. 'failed' e tratado a parte (vira FALHA).
_STATUS_MAP = {
    'sent': 'enviada',
    'delivered': 'entregue',
    'read': 'lida',
}


def _assinatura_valida(request) -> bool:
    """Confere o X-Hub-Signature-256 (HMAC-SHA256 do corpo bruto com o App Secret)."""
    app_secret = (getattr(settings, 'META_WA_APP_SECRET', '') or '').strip()
    if not app_secret:
        # Sem segredo: em desenvolvimento liberamos (facilita testes com ngrok/curl),
        # em producao recusamos.
        if settings.DEBUG:
            return True
        logger.error(
            'Webhook Meta recusado: META_WA_APP_SECRET nao configurado em producao. '
            'Defina o App Secret do app da Meta no ambiente.'
        )
        return False

    recebida = (request.headers.get('X-Hub-Signature-256') or '').strip()
    if not recebida.startswith('sha256='):
        return False
    esperada = hmac.new(app_secret.encode('utf-8'), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(recebida[len('sha256='):], esperada)


def _extrair_texto(message: dict) -> str:
    """Texto "humano" da mensagem recebida, conforme o tipo."""
    tipo = str(message.get('type') or '')
    if tipo == 'text':
        return str((message.get('text') or {}).get('body') or '').strip()
    if tipo == 'button':
        # Resposta a botao de template (quick reply).
        return str((message.get('button') or {}).get('text') or '').strip()
    if tipo == 'interactive':
        interativo = message.get('interactive') or {}
        for chave in ('button_reply', 'list_reply'):
            titulo = str((interativo.get(chave) or {}).get('title') or '').strip()
            if titulo:
                return titulo
        return ''
    if tipo in ('image', 'video', 'document'):
        legenda = str((message.get(tipo) or {}).get('caption') or '').strip()
        return legenda or f'({ {"image": "imagem", "video": "video", "document": "documento"}[tipo] })'
    if tipo == 'audio':
        return '(audio)'
    if tipo == 'sticker':
        return '(figurinha)'
    if tipo == 'location':
        return '(localizacao)'
    if tipo == 'contacts':
        return '(contato)'
    if tipo == 'reaction':
        emoji = str((message.get('reaction') or {}).get('emoji') or '').strip()
        return f'(reacao {emoji})'.strip() if emoji else '(reacao)'
    if tipo == 'unsupported':
        return '(mensagem nao suportada)'
    return ''


def _nome_contato(value: dict, wa_id: str) -> str:
    for contato in value.get('contacts') or []:
        if not isinstance(contato, dict):
            continue
        if str(contato.get('wa_id') or '') == wa_id or not wa_id:
            return str((contato.get('profile') or {}).get('name') or '').strip()
    return ''


def _motivo_falha(status: dict) -> str:
    """Texto de erro para a tela a partir de `statuses[].errors[]`."""
    erros = status.get('errors') or []
    partes = []
    for erro in erros:
        if not isinstance(erro, dict):
            continue
        codigo = erro.get('code')
        titulo = str(erro.get('title') or erro.get('message') or '').strip()
        detalhe = str((erro.get('error_data') or {}).get('details') or '').strip()
        texto = ' - '.join(parte for parte in (titulo, detalhe) if parte)
        partes.append(f'#{codigo}: {texto}' if codigo else texto)
    if not partes:
        return 'A API da Meta reportou falha na entrega.'
    return 'Meta ' + ' | '.join(partes)


@method_decorator(csrf_exempt, name='dispatch')
class MetaWebhookView(View):
    # ------------------------------------------------------------- verificacao
    def get(self, request, *args, **kwargs):
        """Handshake de verificacao: a Meta chama com hub.* e espera o challenge de volta."""
        modo = request.GET.get('hub.mode', '')
        token = request.GET.get('hub.verify_token', '')
        challenge = request.GET.get('hub.challenge', '')
        esperado = (getattr(settings, 'META_WA_VERIFY_TOKEN', '') or '').strip()

        if not (modo or token or challenge):
            # Teste manual no navegador.
            return JsonResponse({'detail': 'Webhook do WhatsApp (Meta) ativo.'}, status=200)

        if modo == 'subscribe' and esperado and hmac.compare_digest(token, esperado):
            return HttpResponse(challenge, content_type='text/plain', status=200)

        logger.warning('Webhook Meta: verificacao recusada (token invalido ou nao configurado).')
        return HttpResponse('Token de verificacao invalido.', status=403)

    # ------------------------------------------------------------------ eventos
    def post(self, request, *args, **kwargs):
        if not _assinatura_valida(request):
            logger.warning('Webhook Meta nao autorizado (assinatura invalida ou ausente).')
            return JsonResponse({'detail': 'Assinatura invalida.'}, status=403)

        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            logger.warning('Webhook Meta: corpo recebido nao e JSON valido.')
            return JsonResponse({'detail': 'JSON invalido.'}, status=400)

        if not isinstance(payload, dict) or payload.get('object') != 'whatsapp_business_account':
            return JsonResponse({'detail': 'Payload ignorado.'}, status=200)

        entradas = 0
        statuses = 0
        houve_falha = False
        for entry in payload.get('entry') or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get('changes') or []:
                if not isinstance(change, dict) or change.get('field') != 'messages':
                    continue
                value = change.get('value') or {}
                if not isinstance(value, dict):
                    continue
                for message in value.get('messages') or []:
                    if isinstance(message, dict) and self._entrada(value, message):
                        entradas += 1
                for status in value.get('statuses') or []:
                    if not isinstance(status, dict):
                        continue
                    resultado = self._status(status)
                    if resultado:
                        statuses += 1
                        if resultado == 'falha':
                            houve_falha = True

        if houve_falha and whatsapp_saude.avaliar_e_pausar_envio():
            logger.error('Webhook Meta: disparo automatico pausado apos falhas consecutivas de envio.')

        # A Meta reenvia o evento se nao receber 200 rapido: sempre confirmamos.
        return JsonResponse({'detail': 'ok', 'entradas': entradas, 'status': statuses}, status=200)

    # ------------------------------------------------------------------ entrada
    def _entrada(self, value: dict, message: dict) -> bool:
        numero = str(message.get('from') or '').strip()
        msg_id = str(message.get('id') or '').strip()
        if not numero:
            logger.warning('Webhook Meta: mensagem sem remetente (from).')
            return False

        # A Meta pode reentregar o mesmo evento (timeout/retry): nao duplicar.
        if msg_id and MensagemContato.objects.filter(
            referencia_externa=msg_id,
            direcao=MensagemContato.DirecaoChoices.ENTRADA,
        ).exists():
            return False

        texto = _extrair_texto(message) or '(mensagem sem texto)'
        push_name = _nome_contato(value, numero)
        agora = timezone.now()
        flip_legado_pendente = False

        pessoa = find_pessoa_by_phone(numero)
        if not pessoa:
            pessoa = PrimeiroContato.objects.create(
                nome=push_name or build_auto_nome_from_phone(numero),
                telefone_whatsapp=phone_for_cadastro(numero),
                primeira_vez=True,
                como_conheceu=PrimeiroContato.ComoConheceuChoices.OUTRO,
                o_que_busca=PrimeiroContato.OQueBuscaChoices.PARTICIPAR_DE_ALGO,
                origem_cadastro=PrimeiroContato.OrigemCadastroChoices.AUTO_CADASTRO,
                iniciou_interacao=True,
                status=PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
                observacoes='Pre-cadastro criado automaticamente via webhook do WhatsApp.',
            )
            InteracaoAcolhimento.objects.create(
                pessoa=pessoa,
                tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
                descricao='Pessoa iniciou a interacao enviando mensagem no WhatsApp.',
                data_interacao=agora.date(),
            )
        elif not pessoa.iniciou_interacao:
            pessoa.iniciou_interacao = True
            pessoa.save(update_fields=['iniciou_interacao', 'atualizado_em'])
            flip_legado_pendente = True
            InteracaoAcolhimento.objects.create(
                pessoa=pessoa,
                tipo=InteracaoAcolhimento.TipoInteracao.RESPOSTA_RECEBIDA,
                descricao='Pessoa respondeu no WhatsApp e liberou o envio de mensagens.',
                data_interacao=agora.date(),
            )

        MensagemContato.objects.create(
            pessoa=pessoa,
            canal=MensagemContato.CanalChoices.WHATSAPP,
            direcao=MensagemContato.DirecaoChoices.ENTRADA,
            status_fila=MensagemContato.StatusFilaChoices.ENVIADA,
            conteudo=texto,
            referencia_externa=msg_id,
            enviada_em=agora,
            entregue_em=agora,
            visualizada_equipe_em=None,
            metadata_resposta={'meta': message},
        )

        ultima_saida = (
            MensagemContato.objects.filter(
                pessoa=pessoa,
                direcao=MensagemContato.DirecaoChoices.SAIDA,
            )
            .order_by('-enviada_em', '-enfileirada_em', '-id')
            .first()
        )
        if ultima_saida:
            ultima_saida.resposta_recebida_em = agora
            ultima_saida.resposta_conteudo = texto
            metadata_resposta = dict(ultima_saida.metadata_resposta or {})
            historico = metadata_resposta.get('meta_webhook', [])
            if not isinstance(historico, list):
                historico = [historico]
            historico.append({'received_at': agora.isoformat(), 'payload': message})
            metadata_resposta['meta_webhook'] = historico
            ultima_saida.metadata_resposta = metadata_resposta
            ultima_saida.save(
                update_fields=[
                    'resposta_recebida_em',
                    'resposta_conteudo',
                    'metadata_resposta',
                    'atualizado_em',
                ]
            )

        # Atendimento automatico (bot de menu). Se ele assumir, controla o status;
        # senao, aplica o flip legado da 1a resposta (pessoa -> Em acompanhamento).
        handled = atendimento_bot.processar_entrada(pessoa, texto)
        if not handled and flip_legado_pendente and pessoa.status in {
            PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
            PrimeiroContato.StatusAcolhimento.ROBO,
        }:
            pessoa.status = PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
            pessoa.save(update_fields=['status', 'atualizado_em'])

        # LGPD: nao logamos telefone nem conteudo da mensagem, apenas identificadores.
        logger.info('Webhook Meta: entrada registrada (pessoa=%s, bot_atendeu=%s).', pessoa.id, handled)
        return True

    # ------------------------------------------------------------------- status
    def _status(self, status: dict) -> str:
        """Atualiza a mensagem de saida. Retorna 'ok' | 'falha' | '' (nao encontrada)."""
        wamid = str(status.get('id') or '').strip()
        status_raw = str(status.get('status') or '').strip().lower()
        if not wamid:
            return ''

        mensagem = MensagemContato.objects.filter(
            referencia_externa=wamid,
            direcao=MensagemContato.DirecaoChoices.SAIDA,
        ).first()
        if not mensagem:
            return ''

        agora = timezone.now()
        update_fields = ['metadata_envio', 'atualizado_em']
        metadata = dict(mensagem.metadata_envio or {})
        historico = metadata.get('meta_webhook', [])
        if not isinstance(historico, list):
            historico = [historico]
        # Guardamos o evento inteiro: o motivo real de uma falha so aparece aqui.
        historico.append({'received_at': agora.isoformat(), 'status': status_raw, 'evento': status})
        metadata['meta_webhook'] = historico
        mensagem.metadata_envio = metadata

        resultado = 'ok'
        if status_raw == 'failed':
            mensagem.status_fila = MensagemContato.StatusFilaChoices.FALHA
            mensagem.erro_ultimo_envio = _motivo_falha(status)
            update_fields.extend(['status_fila', 'erro_ultimo_envio'])
            resultado = 'falha'
        else:
            alvo = _STATUS_MAP.get(status_raw)
            if alvo:
                mensagem.status_fila = MensagemContato.StatusFilaChoices.ENVIADA
                update_fields.append('status_fila')
                if not mensagem.enviada_em:
                    mensagem.enviada_em = agora
                    update_fields.append('enviada_em')
                if alvo in ('entregue', 'lida') and not mensagem.entregue_em:
                    mensagem.entregue_em = agora
                    update_fields.append('entregue_em')
                if alvo == 'lida' and not mensagem.lida_em:
                    mensagem.lida_em = agora
                    update_fields.append('lida_em')

        mensagem.save(update_fields=list(dict.fromkeys(update_fields)))
        return resultado
