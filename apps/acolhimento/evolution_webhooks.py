"""Webhook do Evolution API (entrada de mensagens + status de entrega).

Equivalente aos webhooks da Twilio (`TwilioInboundWebhookView` / `TwilioStatusWebhookView`),
porem no formato de eventos do Evolution (JSON com campo `event`). Um unico endpoint
recebe todos os eventos (WEBHOOK_BY_EVENTS=false).

Eventos tratados:
  messages.upsert  -> mensagem recebida (fromMe=false): cria/atualiza a pessoa e registra a entrada
  messages.update  -> status de entrega/leitura da nossa mensagem enviada
Outros eventos (connection.update, etc.) sao apenas confirmados com 200.
"""
from __future__ import annotations

import json

from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato
from apps.acolhimento.phone_utils import (
    build_auto_nome_from_phone,
    find_pessoa_by_phone,
    phone_for_cadastro,
)


def _normalizar_evento(payload: dict) -> str:
    return (payload.get('event') or '').replace('-', '.').strip().lower()


def _sanitizar(payload: dict) -> dict:
    """Remove a apikey que o Evolution inclui no corpo do webhook antes de persistir."""
    if not isinstance(payload, dict):
        return {}
    return {chave: valor for chave, valor in payload.items() if chave != 'apikey'}


def _primeiro(data):
    """data do evento pode vir como dict (1 msg) ou list; normaliza para dict."""
    if isinstance(data, list):
        return data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _extrair_texto(message: dict) -> str:
    if not isinstance(message, dict):
        return ''
    if message.get('conversation'):
        return message['conversation']
    texto = ((message.get('extendedTextMessage') or {}).get('text') or '').strip()
    if texto:
        return texto
    for chave in ('imageMessage', 'videoMessage', 'documentMessage'):
        legenda = ((message.get(chave) or {}).get('caption') or '').strip()
        if legenda:
            return legenda
    if message.get('audioMessage'):
        return '(audio)'
    if message.get('stickerMessage'):
        return '(figurinha)'
    if message.get('locationMessage'):
        return '(localizacao)'
    return ''


# Status do Baileys -> alvo interno. Aceita nomes e codigos numericos.
_STATUS_MAP = {
    'SERVER_ACK': 'enviada', '2': 'enviada',
    'DELIVERY_ACK': 'entregue', '3': 'entregue',
    'READ': 'lida', '4': 'lida',
    'PLAYED': 'lida', '5': 'lida',
}


@method_decorator(csrf_exempt, name='dispatch')
class EvolutionWebhookView(View):
    def get(self, request, *args, **kwargs):
        # Facilita um teste manual no navegador; o Evolution sempre usa POST.
        return JsonResponse({'detail': 'Webhook do WhatsApp ativo. Use POST.'}, status=200)

    def post(self, request, *args, **kwargs):
        try:
            payload = json.loads(request.body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({'detail': 'JSON invalido.'}, status=400)

        if not isinstance(payload, dict):
            return JsonResponse({'detail': 'Payload inesperado.'}, status=400)

        evento = _normalizar_evento(payload)
        data = _primeiro(payload.get('data'))

        if evento == 'messages.upsert':
            return self._entrada(payload, data)
        if evento in ('messages.update', 'messages.set'):
            return self._status(payload, payload.get('data'))

        return JsonResponse({'detail': f'evento ignorado: {evento or "?"}'}, status=200)

    # ------------------------------------------------------------------ entrada
    def _entrada(self, payload: dict, data: dict) -> JsonResponse:
        key = data.get('key') or {}
        if key.get('fromMe'):
            return JsonResponse({'detail': 'ignorado (mensagem propria).'}, status=200)

        remote_jid = key.get('remoteJid') or ''
        if remote_jid.endswith('@g.us') or remote_jid.startswith('status@'):
            return JsonResponse({'detail': 'ignorado (grupo/status).'}, status=200)

        numero = remote_jid.split('@', 1)[0]
        if not numero:
            return JsonResponse({'detail': 'remoteJid ausente.'}, status=400)

        texto = _extrair_texto(data.get('message') or {}) or '(mensagem sem texto)'
        msg_id = key.get('id') or ''
        push_name = (data.get('pushName') or '').strip()
        payload_safe = _sanitizar(payload)
        agora = timezone.now()

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
            update_fields = ['iniciou_interacao', 'atualizado_em']
            if pessoa.status in {
                PrimeiroContato.StatusAcolhimento.PRIMEIRO_CONTATO,
                PrimeiroContato.StatusAcolhimento.ROBO,
            }:
                pessoa.status = PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
                update_fields.append('status')
            pessoa.save(update_fields=update_fields)
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
            metadata_resposta={'evolution': payload_safe},
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
            historico = metadata_resposta.get('evolution_webhook', [])
            if not isinstance(historico, list):
                historico = [historico]
            historico.append({'received_at': agora.isoformat(), 'payload': payload_safe})
            metadata_resposta['evolution_webhook'] = historico
            ultima_saida.metadata_resposta = metadata_resposta
            ultima_saida.save(
                update_fields=[
                    'resposta_recebida_em',
                    'resposta_conteudo',
                    'metadata_resposta',
                    'atualizado_em',
                ]
            )

        return JsonResponse({'detail': 'Mensagem de entrada processada.', 'pessoa_id': pessoa.id}, status=200)

    # ------------------------------------------------------------------- status
    def _status(self, payload: dict, data) -> JsonResponse:
        itens = data if isinstance(data, list) else [data]
        atualizadas = 0

        for item in itens:
            if not isinstance(item, dict):
                continue
            key = item.get('key') or {}
            key_id = item.get('keyId') or key.get('id') or item.get('id') or ''
            status_raw = str(item.get('status') or '').upper()
            if not key_id:
                continue

            mensagem = MensagemContato.objects.filter(
                referencia_externa=key_id,
                direcao=MensagemContato.DirecaoChoices.SAIDA,
            ).first()
            if not mensagem:
                continue

            agora = timezone.now()
            update_fields = ['metadata_envio', 'atualizado_em']
            metadata = dict(mensagem.metadata_envio or {})
            historico = metadata.get('evolution_webhook', [])
            if not isinstance(historico, list):
                historico = [historico]
            historico.append({'received_at': agora.isoformat(), 'status': status_raw})
            metadata['evolution_webhook'] = historico
            mensagem.metadata_envio = metadata

            if status_raw == 'ERROR':
                mensagem.status_fila = MensagemContato.StatusFilaChoices.FALHA
                mensagem.erro_ultimo_envio = 'A API de WhatsApp reportou erro no envio.'
                update_fields.extend(['status_fila', 'erro_ultimo_envio'])
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
            atualizadas += 1

        return JsonResponse({'detail': 'Status processado.', 'atualizadas': atualizadas}, status=200)
