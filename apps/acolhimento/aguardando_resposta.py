"""Quem esta aguardando resposta da equipe no WhatsApp.

A pessoa fica "aguardando resposta" quando a ultima mensagem que ela mandou e mais
nova que a ultima resposta da equipe (ou ninguem respondeu ainda). Varias mensagens
seguidas contam como uma pendencia so; assim que alguem da equipe responde, ela sai
da lista (e volta se mandar outra mensagem).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db.models import DateTimeField, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.timesince import timesince

from apps.acolhimento.models import MensagemContato, TemplateWhatsapp

# Cadastrada nos ultimos N dias = "pessoa nova".
DIAS_PESSOA_NOVA = 30
# A aba "Respondidas" mostra as respostas da equipe dos ultimos N dias.
DIAS_RESPONDIDAS = 7
# Esperando mais que isso: destaque de atraso no card.
HORAS_ESPERA_LONGA = 24

# Marca (em metadata_envio) das mensagens livres do disparo em massa.
ORIGEM_DISPARO_MASSA = 'disparo_massa'
# Templates enviados em lote: nao respondem ninguem em particular.
TEMPLATES_EM_LOTE = [TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO, 'marketing_massa']
# Envio que nao saiu nao conta como resposta: a pessoa volta para a lista.
STATUS_SEM_ENVIO = [
    MensagemContato.StatusFilaChoices.FALHA,
    MensagemContato.StatusFilaChoices.CANCELADA,
]

_NUNCA_RESPONDIDA = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)


def respostas_da_equipe():
    """Mensagens de saida que contam como resposta da equipe.

    Vale o que alguem da equipe mandou para a pessoa: conversa, template de
    continuacao, link de questionario. Nao valem o robo (sem autor), os envios em
    lote (boas-vindas e disparo em massa) nem o que falhou ou foi cancelado.
    """
    # O has_key vem junto da comparacao: sem ele, mensagem sem a chave daria NULL e
    # o exclude descartaria tambem as respostas comuns (metadata vazio).
    em_lote = Q(
        metadata_envio__has_key='tipo_template',
        metadata_envio__tipo_template__in=TEMPLATES_EM_LOTE,
    ) | Q(
        metadata_envio__has_key='origem',
        metadata_envio__origem=ORIGEM_DISPARO_MASSA,
    )
    return (
        MensagemContato.objects.filter(
            direcao=MensagemContato.DirecaoChoices.SAIDA,
            criado_por__isnull=False,
        )
        .exclude(status_fila__in=STATUS_SEM_ENVIO)
        .exclude(em_lote)
    )


def anotar_conversa(queryset):
    """Anota em cada pessoa: ultima_entrada, ultima_resposta (+ id) e aguardando_desde."""
    entradas = MensagemContato.objects.filter(
        pessoa=OuterRef('pk'),
        direcao=MensagemContato.DirecaoChoices.ENTRADA,
    )
    respostas = respostas_da_equipe().filter(pessoa=OuterRef('pk')).order_by('-enfileirada_em', '-id')
    queryset = queryset.annotate(
        ultima_entrada=Subquery(entradas.order_by('-enfileirada_em', '-id').values('enfileirada_em')[:1]),
        ultima_resposta=Subquery(respostas.values('enfileirada_em')[:1]),
        ultima_resposta_id=Subquery(respostas.values('id')[:1]),
    )
    # A primeira mensagem ainda sem resposta diz desde quando a pessoa espera.
    depois_da_resposta = Coalesce(
        OuterRef('ultima_resposta'),
        Value(_NUNCA_RESPONDIDA, output_field=DateTimeField()),
    )
    return queryset.annotate(
        aguardando_desde=Subquery(
            entradas.filter(enfileirada_em__gt=depois_da_resposta)
            .order_by('enfileirada_em', 'id')
            .values('enfileirada_em')[:1]
        ),
    )


def filtrar_aguardando(queryset):
    """Pessoas (queryset anotado) cuja ultima mensagem ainda nao teve resposta."""
    return queryset.filter(ultima_entrada__isnull=False).filter(
        Q(ultima_resposta__isnull=True) | Q(ultima_entrada__gt=F('ultima_resposta'))
    )


def filtrar_respondidas(queryset, dias=DIAS_RESPONDIDAS):
    """Pessoas (queryset anotado) que a equipe respondeu nos ultimos `dias`."""
    return queryset.filter(
        ultima_entrada__isnull=False,
        ultima_resposta__gte=F('ultima_entrada'),
    ).filter(ultima_resposta__gte=timezone.now() - timedelta(days=dias))


def corte_pessoa_nova():
    """Quem foi cadastrado a partir desta data conta como pessoa nova."""
    return timezone.localdate() - timedelta(days=DIAS_PESSOA_NOVA)


def tempo_espera(desde, agora=None):
    """Texto curto para o card: 'agora', 'ha 5 minutos', 'ha 2 dias'."""
    if not desde:
        return ''
    agora = agora or timezone.now()
    if agora - desde < timedelta(minutes=1):
        return 'agora'
    return f'há {timesince(desde, now=agora, depth=1)}'


def detalhar_aguardando(pessoas):
    """Resume, em cada pessoa da pagina, as mensagens que ainda estao sem resposta.

    Preenche `qtd_aguardando`, `ultima_mensagem`, `tem_nao_lida` (chegou mensagem
    que ninguem da equipe abriu), `espera` (texto) e `espera_longa`.
    """
    if not pessoas:
        return

    sem_resposta = Q()
    for pessoa in pessoas:
        da_pessoa = Q(pessoa_id=pessoa.pk)
        if pessoa.ultima_resposta:
            da_pessoa &= Q(enfileirada_em__gt=pessoa.ultima_resposta)
        sem_resposta |= da_pessoa

    por_pessoa = defaultdict(list)
    mensagens = (
        MensagemContato.objects.filter(sem_resposta, direcao=MensagemContato.DirecaoChoices.ENTRADA)
        .order_by('enfileirada_em', 'id')
        .values('pessoa_id', 'conteudo', 'visualizada_equipe_em')
    )
    for mensagem in mensagens:
        por_pessoa[mensagem['pessoa_id']].append(mensagem)

    agora = timezone.now()
    for pessoa in pessoas:
        pendentes = por_pessoa[pessoa.pk]
        pessoa.qtd_aguardando = len(pendentes)
        pessoa.ultima_mensagem = pendentes[-1]['conteudo'] if pendentes else ''
        pessoa.tem_nao_lida = any(mensagem['visualizada_equipe_em'] is None for mensagem in pendentes)
        pessoa.espera = tempo_espera(pessoa.aguardando_desde, agora)
        pessoa.espera_longa = bool(
            pessoa.aguardando_desde
            and agora - pessoa.aguardando_desde > timedelta(hours=HORAS_ESPERA_LONGA)
        )


def detalhar_respondidas(pessoas):
    """Preenche `respondida_por` (quem da equipe mandou a ultima resposta) e `espera`."""
    respostas = MensagemContato.objects.select_related('criado_por').in_bulk(
        [pessoa.ultima_resposta_id for pessoa in pessoas if pessoa.ultima_resposta_id]
    )
    agora = timezone.now()
    for pessoa in pessoas:
        resposta = respostas.get(pessoa.ultima_resposta_id)
        autor = resposta.criado_por if resposta else None
        pessoa.respondida_por = (autor.get_full_name() or autor.get_username()) if autor else ''
        pessoa.espera = tempo_espera(pessoa.ultima_resposta, agora)
