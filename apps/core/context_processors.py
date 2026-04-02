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
