"""Tela "Conversas da IA": o que a IA fez com cada mensagem que recebeu.

Cada atendimento e uma mensagem da pessoa com metadata_resposta['ia'] (gravado por
atendimento_ia.registrar): a decisao, a mensagem que saiu (resposta_id), modelo,
tokens e erro. Respostas antigas foram copiadas pela migracao 0036.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.acolhimento.models import MensagemContato

ACOES = {
    'responder': ('Respondeu', 'ok'),
    'opcao': ('Mandou uma opção do menu', 'ok'),
    'transferir': ('Passou para a equipe', 'equipe'),
    'limite': ('Limite do dia: passou para a equipe', 'equipe'),
    'falha': ('IA falhou: o robô mandou o menu', 'erro'),
    'ilegivel': ('Resposta fora do formato: o robô mandou o menu', 'erro'),
}

FILTROS = {
    'resolvidas': ('Resolvidas pela IA', ['responder', 'opcao']),
    'equipe': ('Passadas para a equipe', ['transferir', 'limite']),
    'falhas': ('Falhas', ['falha', 'ilegivel']),
}

PERIODOS = (1, 7, 30)
PERIODO_PADRAO = 7


def atendimentos(*, dias: int | None = PERIODO_PADRAO, filtro: str = '', busca: str = ''):
    queryset = MensagemContato.objects.filter(
        direcao=MensagemContato.DirecaoChoices.ENTRADA,
        metadata_resposta__has_key='ia',
    ).select_related('pessoa')
    if dias:
        queryset = queryset.filter(enfileirada_em__gte=timezone.now() - timedelta(days=dias))
    if filtro in FILTROS:
        queryset = queryset.filter(metadata_resposta__ia__acao__in=FILTROS[filtro][1])
    busca = (busca or '').strip()
    if busca:
        queryset = queryset.filter(
            Q(pessoa__nome__icontains=busca) | Q(conteudo__icontains=busca) | Q(transcricao__icontains=busca)
        )
    return queryset.order_by('-enfileirada_em', '-id')


def resumo(queryset) -> dict:
    acoes = Counter()
    tokens_entrada = tokens_saida = audios = 0
    for metadata, midia_tipo in queryset.values_list('metadata_resposta', 'midia_tipo'):
        ia = (metadata or {}).get('ia') or {}
        acoes[ia.get('acao') or '?'] += 1
        tokens_entrada += int(ia.get('tokens_entrada') or 0)
        tokens_saida += int(ia.get('tokens_saida') or 0)
        audios += midia_tipo == MensagemContato.MidiaTipoChoices.AUDIO
    total = sum(acoes.values())
    resolvidas = acoes['responder'] + acoes['opcao']
    consultas = total - acoes['limite'] - acoes['falha']
    return {
        'total': total,
        'resolvidas': resolvidas,
        'pct_resolvidas': round(100 * resolvidas / total) if total else None,
        'equipe': acoes['transferir'] + acoes['limite'],
        'falhas': acoes['falha'] + acoes['ilegivel'],
        'audios': audios,
        'tokens_entrada': tokens_entrada,
        'tokens_saida': tokens_saida,
        'tokens_por_resposta': round((tokens_entrada + tokens_saida) / consultas) if consultas else None,
    }


def detalhar(mensagens) -> None:
    """Preenche em cada mensagem da pagina: ia, rotulo, classe, pergunta, eh_audio e resposta."""
    mensagens = list(mensagens)
    ids = [((m.metadata_resposta or {}).get('ia') or {}).get('resposta_id') for m in mensagens]
    respostas = MensagemContato.objects.in_bulk([i for i in ids if i])
    for mensagem in mensagens:
        ia = (mensagem.metadata_resposta or {}).get('ia') or {}
        mensagem.ia = ia
        mensagem.rotulo_acao, mensagem.classe_acao = ACOES.get(ia.get('acao'), ('?', 'erro'))
        mensagem.eh_audio = mensagem.midia_tipo == MensagemContato.MidiaTipoChoices.AUDIO
        mensagem.pergunta = (mensagem.transcricao or '').strip() or (mensagem.conteudo or '').strip()
        mensagem.resposta = respostas.get(ia.get('resposta_id'))
        mensagem.tokens = int(ia.get('tokens_entrada') or 0) + int(ia.get('tokens_saida') or 0)
