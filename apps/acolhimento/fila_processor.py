import re
from typing import Callable

from django.utils import timezone

from apps.acolhimento.models import MensagemContato
from apps.acolhimento.twilio_service import TwilioWhatsAppError, send_whatsapp_message


ProgressCallback = Callable[[str], None]
ShouldStopCallback = Callable[[], bool]


def normalize_phone_br(raw_phone: str) -> str:
    raw_phone = (raw_phone or '').strip()
    if not raw_phone:
        raise ValueError('Telefone vazio.')

    if raw_phone.startswith('+'):
        digits = re.sub(r'\D', '', raw_phone)
        if not digits:
            raise ValueError('Telefone invalido.')
        return f'+{digits}'

    digits = re.sub(r'\D', '', raw_phone)
    if not digits:
        raise ValueError('Telefone invalido.')

    if digits.startswith('55') and len(digits) in (12, 13):
        return f'+{digits}'

    if len(digits) in (10, 11):
        return f'+55{digits}'

    raise ValueError('Telefone fora do formato esperado para Brasil.')


def processar_fila_mensagens(
    *,
    limit: int = 20,
    ids: list[int] | None = None,
    dry_run: bool = False,
    progress_callback: ProgressCallback | None = None,
    should_stop: ShouldStopCallback | None = None,
) -> dict:
    ids = ids or []
    limit = max(int(limit), 0)

    queryset = (
        MensagemContato.objects.select_related('pessoa')
        .filter(
            status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
            canal=MensagemContato.CanalChoices.WHATSAPP,
            direcao=MensagemContato.DirecaoChoices.SAIDA,
        )
        .order_by('prioridade', 'enfileirada_em', 'id')
    )

    if ids:
        queryset = queryset.filter(id__in=ids)

    mensagens = list(queryset[:limit]) if limit else list(queryset)

    if progress_callback:
        progress_callback(f'Total selecionado: {len(mensagens)}')
        if dry_run:
            progress_callback('Executando em DRY RUN (sem envio real).')

    sucesso = 0
    falha = 0
    processadas = 0
    interrompido = False

    for mensagem in mensagens:
        if should_stop and should_stop():
            interrompido = True
            if progress_callback:
                progress_callback('Processamento interrompido por solicitacao do usuario.')
            break

        try:
            destino = normalize_phone_br(mensagem.pessoa.telefone_whatsapp)
        except ValueError as exc:
            if not dry_run:
                mensagem.status_fila = MensagemContato.StatusFilaChoices.FALHA
                mensagem.tentativas_envio += 1
                mensagem.erro_ultimo_envio = str(exc)
                metadata = dict(mensagem.metadata_envio or {})
                metadata['twilio'] = {'error': str(exc)}
                mensagem.metadata_envio = metadata
                mensagem.save(
                    update_fields=[
                        'status_fila',
                        'tentativas_envio',
                        'erro_ultimo_envio',
                        'metadata_envio',
                        'atualizado_em',
                    ]
                )
            falha += 1
            processadas += 1
            if progress_callback:
                progress_callback(f'[{mensagem.id}] Falha: {exc}')
            continue

        if dry_run:
            processadas += 1
            if progress_callback:
                progress_callback(
                    f'[{mensagem.id}] DRY RUN -> destino={destino} conteudo={mensagem.conteudo[:60]}'
                )
            continue

        mensagem.status_fila = MensagemContato.StatusFilaChoices.PROCESSANDO
        mensagem.tentativas_envio += 1
        mensagem.save(update_fields=['status_fila', 'tentativas_envio', 'atualizado_em'])

        try:
            template_cfg = dict((mensagem.metadata_envio or {}).get('twilio_template') or {})
            content_sid = (template_cfg.get('content_sid') or '').strip()
            content_variables = (template_cfg.get('content_variables') or '').strip() if template_cfg else None

            if content_sid:
                resultado = send_whatsapp_message(
                    to_phone=destino,
                    content_sid=content_sid,
                    content_variables=content_variables,
                )
            else:
                resultado = send_whatsapp_message(to_phone=destino, body=mensagem.conteudo)
        except TwilioWhatsAppError as exc:
            mensagem.status_fila = MensagemContato.StatusFilaChoices.FALHA
            mensagem.erro_ultimo_envio = str(exc)
            metadata = dict(mensagem.metadata_envio or {})
            metadata['twilio'] = {'error': str(exc)}
            mensagem.metadata_envio = metadata
            mensagem.save(
                update_fields=[
                    'status_fila',
                    'erro_ultimo_envio',
                    'metadata_envio',
                    'atualizado_em',
                ]
            )
            falha += 1
            processadas += 1
            if progress_callback:
                progress_callback(f'[{mensagem.id}] Falha no envio: {exc}')
            continue

        twilio_status = (resultado.get('status') or '').lower()
        if twilio_status in {'queued', 'accepted', 'sending'}:
            mensagem.status_fila = MensagemContato.StatusFilaChoices.PROCESSANDO
            mensagem.enviada_em = None
        else:
            mensagem.status_fila = MensagemContato.StatusFilaChoices.ENVIADA
            mensagem.enviada_em = timezone.now()
        mensagem.referencia_externa = resultado.get('sid', '') or ''
        mensagem.erro_ultimo_envio = ''
        metadata = dict(mensagem.metadata_envio or {})
        metadata['twilio'] = resultado
        metadata['destino_normalizado'] = destino
        mensagem.metadata_envio = metadata
        mensagem.save(
            update_fields=[
                'status_fila',
                'enviada_em',
                'referencia_externa',
                'erro_ultimo_envio',
                'metadata_envio',
                'atualizado_em',
            ]
        )
        sucesso += 1
        processadas += 1
        if progress_callback:
            progress_callback(
                f'[{mensagem.id}] Twilio status={twilio_status or "desconhecido"} SID={mensagem.referencia_externa}'
            )

    resumo = f'Processamento finalizado. sucesso={sucesso} falha={falha} total={len(mensagens)}'
    if progress_callback:
        progress_callback(resumo)

    return {
        'total_selecionado': len(mensagens),
        'total_processado': processadas,
        'sucesso': sucesso,
        'falha': falha,
        'interrompido': interrompido,
        'resumo': resumo,
    }
