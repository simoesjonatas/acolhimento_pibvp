def mensagens_retorno_pendente(request):
    if not request.user.is_authenticated:
        return {'menu_retorno_pendente_count': 0}

    if not (request.user.is_staff or request.user.is_superuser):
        return {'menu_retorno_pendente_count': 0}

    from apps.acolhimento.models import MensagemContato

    total = MensagemContato.objects.filter(
        direcao=MensagemContato.DirecaoChoices.ENTRADA,
        visualizada_equipe_em__isnull=True,
    ).count()

    return {'menu_retorno_pendente_count': total}


def whatsapp_conexao(request):
    """Indicador discreto (no menu) do estado da conexao do WhatsApp.

    Modo Evolution: estado da sessao pareada (open/connecting/close). Modo Meta
    (API oficial): se o Graph API responde pelo numero configurado. No modo Twilio
    retorna vazio (nao renderiza). O estado e cacheado por 30s para nao chamar a
    API a cada requisicao.
    """
    from django.conf import settings

    provider = (getattr(settings, 'WHATSAPP_PROVIDER', 'twilio') or '').strip().lower()
    if provider not in ('evolution', 'meta'):
        return {}
    if not request.user.is_authenticated:
        return {}

    from django.core.cache import cache

    estado = cache.get('wa_conn_estado', '__miss__')
    if estado == '__miss__':
        if provider == 'meta':
            from apps.acolhimento import meta_service

            if meta_service.configurado():
                estado = 'open' if meta_service.get_phone_number_info(timeout=2) is not None else None
            else:
                estado = 'close'
        else:
            from apps.acolhimento import evolution_service

            estado = evolution_service.get_connection_state(timeout=2)
        cache.set('wa_conn_estado', estado, 30)

    if estado == 'open':
        classe, texto = 'ok', 'conectado'
    elif estado == 'connecting':
        classe, texto = 'wait', 'conectando'
    elif estado is None:
        classe, texto = 'off', 'API fora do ar'
    elif provider == 'meta':
        classe, texto = 'off', 'nao configurado'
    else:
        classe, texto = 'off', 'desconectado'

    # A conexao seguir "open" nao quer dizer que as mensagens estao saindo: com o erro
    # 463 (enderecamento LID / conta restrita) o WhatsApp aceita a conexao e recusa cada
    # envio. Sem este aviso o indicador fica verde enquanto nada e entregue.
    falhas = cache.get('wa_falhas_recentes', '__miss__')
    if falhas == '__miss__':
        from apps.acolhimento import whatsapp_saude

        falhas = whatsapp_saude.falhas_recentes(horas=24)
        cache.set('wa_falhas_recentes', falhas, 60)

    if estado == 'open' and falhas:
        classe = 'wait'
        texto = f'conectado, mas {falhas} envio(s) falharam em 24h'

    return {
        'whatsapp_provider': provider,
        'whatsapp_estado': estado,
        'whatsapp_estado_classe': classe,
        'whatsapp_estado_texto': texto,
        'whatsapp_falhas_recentes': falhas,
    }
