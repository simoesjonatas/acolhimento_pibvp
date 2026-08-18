import re
from typing import Callable

from django.db.models import F
from django.utils import timezone

from apps.acolhimento.models import MensagemContato
from apps.acolhimento import whatsapp_gateway as gateway


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


def _reivindicar_mensagem(mensagem: MensagemContato) -> bool:
    """Claim atomico PENDENTE -> PROCESSANDO. Retorna True se ESTE processo assumiu.

    Usa um UPDATE condicional (em vez de SELECT ... FOR UPDATE) para se comportar
    igual em SQLite (dev/testes) e Postgres (producao): numa corrida entre
    processadores, so um consegue rowcount=1; os demais recebem False e pulam a
    mensagem, evitando envio duplicado quando ha varios workers/cron ao mesmo tempo.
    """
    agora = timezone.now()
    reivindicada = MensagemContato.objects.filter(
        pk=mensagem.pk,
        status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
    ).update(
        status_fila=MensagemContato.StatusFilaChoices.PROCESSANDO,
        tentativas_envio=F('tentativas_envio') + 1,
        atualizado_em=agora,
    )
    if not reivindicada:
        return False
    # O UPDATE nao mexe na instancia em memoria; refletir para os passos seguintes.
    mensagem.status_fila = MensagemContato.StatusFilaChoices.PROCESSANDO
    mensagem.tentativas_envio = (mensagem.tentativas_envio or 0) + 1
    mensagem.atualizado_em = agora
    return True


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

        bloqueio = gateway.motivo_bloqueio_mensagem(mensagem)
        if bloqueio:
            motivo_codigo, motivo_texto = bloqueio
            if not dry_run:
                mensagem.status_fila = MensagemContato.StatusFilaChoices.CANCELADA
                mensagem.erro_ultimo_envio = motivo_texto
                metadata = dict(mensagem.metadata_envio or {})
                metadata['bloqueio_envio'] = {
                    'motivo': motivo_codigo,
                    'registrado_em': timezone.now().isoformat(),
                }
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
                progress_callback(f'[{mensagem.id}] Bloqueada: {motivo_texto}')
            continue

        try:
            destino = normalize_phone_br(mensagem.pessoa.telefone_whatsapp)
        except ValueError as exc:
            if not dry_run:
                mensagem.status_fila = MensagemContato.StatusFilaChoices.FALHA
                mensagem.tentativas_envio += 1
                mensagem.erro_ultimo_envio = str(exc)
                metadata = dict(mensagem.metadata_envio or {})
                metadata[gateway.provider_key()] = {'error': str(exc)}
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

        if not _reivindicar_mensagem(mensagem):
            # Corrida perdida: outro processador ja assumiu esta mensagem.
            # Nao reenviar (garante 1 envio por mensagem entre workers concorrentes).
            if progress_callback:
                progress_callback(f'[{mensagem.id}] Ignorada: assumida por outro processador.')
            continue

        try:
            envelope = gateway.enviar(mensagem, destino)
        except gateway.WhatsAppSendError as exc:
            mensagem.status_fila = MensagemContato.StatusFilaChoices.FALHA
            mensagem.erro_ultimo_envio = str(exc)
            metadata = dict(mensagem.metadata_envio or {})
            metadata[gateway.provider_key()] = {'error': str(exc)}
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

        mensagem.status_fila = envelope['status_fila']
        mensagem.enviada_em = envelope['enviada_em']
        mensagem.referencia_externa = envelope['referencia_externa'] or ''
        mensagem.erro_ultimo_envio = ''
        metadata = dict(mensagem.metadata_envio or {})
        metadata[envelope['metadata_key']] = envelope['metadata_value']
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
                f"[{mensagem.id}] {envelope['provider']} status={envelope['status_label']} ref={mensagem.referencia_externa}"
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
