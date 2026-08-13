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
    """Indicador discreto (no menu) do estado da conexao do WhatsApp via Evolution.

    So faz sentido no modo Evolution; no modo Twilio retorna vazio (nao renderiza).
    O estado e cacheado por 30s para nao chamar o Evolution a cada requisicao.
    """
    from django.conf import settings

    if (getattr(settings, 'WHATSAPP_PROVIDER', 'twilio') or '').strip().lower() != 'evolution':
        return {}
    if not request.user.is_authenticated:
        return {}

    from django.core.cache import cache
    from apps.acolhimento import evolution_service

    estado = cache.get('wa_conn_estado', '__miss__')
    if estado == '__miss__':
        estado = evolution_service.get_connection_state(timeout=2)
        cache.set('wa_conn_estado', estado, 30)

    if estado == 'open':
        classe, texto = 'ok', 'conectado'
    elif estado == 'connecting':
        classe, texto = 'wait', 'conectando'
    elif estado is None:
        classe, texto = 'off', 'API fora do ar'
    else:
        classe, texto = 'off', 'desconectado'

    return {
        'whatsapp_provider': 'evolution',
        'whatsapp_estado': estado,
        'whatsapp_estado_classe': classe,
        'whatsapp_estado_texto': texto,
    }
