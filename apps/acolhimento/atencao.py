"""Quem precisa de atencao: pessoas paradas na mesma etapa sem contato da equipe.

"Ultimo contato" e o mais recente entre:
- um passo registrado na ficha (InteracaoAcolhimento; mudar o status tambem registra);
- uma mensagem de WhatsApp enfileirada por alguem da equipe (criado_por preenchido),
  sem contar as que falharam ou foram canceladas. Respostas do robo/IA nao contam;
- o cadastro, para quem ainda nao teve nada (assim ninguem aparece no dia em que chega).

Cada etapa tem um prazo (PRAZO_DIAS). Primeiro contato fica de fora (e o botao de
boas-vindas que cuida dele) e Membro tambem (o acolhimento terminou).
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import DateTimeField, F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato

Status = PrimeiroContato.StatusAcolhimento

# Dias sem contato da equipe ate a pessoa entrar em "Precisam de atencao".
PRAZO_DIAS = {
    Status.ROBO: 7,
    Status.EM_ACOMPANHAMENTO: 14,
    Status.PARTICIPANTE: 14,
    Status.EM_GABINETE: 14,
    Status.APROVADO: 30,
}

# Etapas em que a pessoa ainda esta sendo acolhida (aparecem em "Meus acolhidos").
EM_ACOLHIMENTO = [Status.PRIMEIRO_CONTATO, *PRAZO_DIAS]


def anotar_ultimo_contato(queryset):
    """Anota `ultimo_contato` (datetime) em um queryset de PrimeiroContato."""
    ultima_interacao = (
        InteracaoAcolhimento.objects.filter(pessoa=OuterRef('pk'))
        .order_by('-criado_em')
        .values('criado_em')[:1]
    )
    ultima_mensagem_equipe = (
        MensagemContato.objects.filter(
            pessoa=OuterRef('pk'),
            direcao=MensagemContato.DirecaoChoices.SAIDA,
            criado_por__isnull=False,
        )
        .exclude(status_fila__in=[
            MensagemContato.StatusFilaChoices.FALHA,
            MensagemContato.StatusFilaChoices.CANCELADA,
        ])
        .order_by('-enfileirada_em')
        .values('enfileirada_em')[:1]
    )
    # Coalesce em cada termo: no SQLite o GREATEST com um NULL da NULL.
    return queryset.annotate(
        ultimo_contato=Greatest(
            Coalesce(Subquery(ultima_interacao, output_field=DateTimeField()), F('criado_em')),
            Coalesce(Subquery(ultima_mensagem_equipe, output_field=DateTimeField()), F('criado_em')),
        ),
    )


def filtro_atrasados(agora=None) -> Q:
    """Q para um queryset ja anotado com `ultimo_contato`."""
    agora = agora or timezone.now()
    filtro = Q(pk__in=[])
    for status, dias in PRAZO_DIAS.items():
        filtro |= Q(status=status, ultimo_contato__lt=agora - timedelta(days=dias))
    return filtro


def precisam_de_atencao(queryset=None):
    """Pessoas fora do prazo, as que estao paradas ha mais tempo primeiro."""
    queryset = PrimeiroContato.objects.all() if queryset is None else queryset
    return (
        anotar_ultimo_contato(queryset)
        .filter(filtro_atrasados())
        .order_by('ultimo_contato', 'pk')
    )


def dias_sem_contato(pessoa, agora=None) -> int | None:
    ultimo = getattr(pessoa, 'ultimo_contato', None)
    if ultimo is None:
        return None
    return max(((agora or timezone.now()) - ultimo).days, 0)


def atrasada(pessoa, agora=None) -> bool:
    prazo = PRAZO_DIAS.get(pessoa.status)
    dias = dias_sem_contato(pessoa, agora)
    return prazo is not None and dias is not None and dias >= prazo


def marcar(pessoas, agora=None):
    """Preenche `dias_sem_contato`, `prazo_dias` e `atrasada` em cada pessoa (para o template)."""
    agora = agora or timezone.now()
    for pessoa in pessoas:
        pessoa.dias_sem_contato = dias_sem_contato(pessoa, agora)
        pessoa.prazo_dias = PRAZO_DIAS.get(pessoa.status)
        pessoa.atrasada = atrasada(pessoa, agora)
    return pessoas
