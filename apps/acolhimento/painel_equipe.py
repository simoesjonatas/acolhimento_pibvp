"""Painel da equipe (lider): como esta cada voluntario com os proprios acolhidos.

Por responsavel, olhando as pessoas ainda em acolhimento (Primeiro contato ate
Aprovado):
- acolhidos: quantas pessoas estao com ele;
- atrasados: fora do prazo da etapa (mesma regra de "Precisam de atencao");
- aguardando: mandaram mensagem e ninguem da equipe respondeu ainda (mesma regra da
  tela "Aguardando resposta"); "ha mais de 24h" e o que ja perdeu a janela;
- resposta: nas pessoas dele, quanto tempo a equipe leva para responder (mediana) e
  quantas mensagens foram respondidas em ate 24h, no periodo escolhido.

O tempo de resposta conta a resposta de qualquer pessoa da equipe, nao so do
responsavel: o que importa para quem escreveu e ser respondido.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from statistics import median

from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.acolhimento import atencao, rodizio
from apps.acolhimento.aguardando_resposta import anotar_conversa, respostas_da_equipe
from apps.acolhimento.models import MensagemContato, PrimeiroContato

PERIODOS = (7, 30, 90)
PERIODO_PADRAO = 30
JANELA = timedelta(hours=24)


def _linha_vazia():
    return {
        'acolhidos': 0,
        'atrasados': 0,
        'aguardando': 0,
        'aguardando_24h': 0,
        'espera_mais_antiga': None,
        'tempos': [],
        'pendencias': 0,
        'em_24h': 0,
    }


def _pessoas_em_acolhimento():
    return anotar_conversa(
        atencao.anotar_ultimo_contato(
            PrimeiroContato.objects.filter(status__in=atencao.EM_ACOLHIMENTO)
        )
    ).only('id', 'status', 'responsavel_atual', 'criado_em')


def _tempos_de_resposta(responsavel_por_pessoa, inicio, agora):
    """{responsavel_id: (tempos respondidos, pendencias, respondidas em 24h)} no periodo.

    Cada pendencia comeca na primeira mensagem da pessoa depois da ultima resposta
    da equipe e termina na proxima resposta. Pendencia ainda aberta ha menos de 24h
    nao entra na conta (ainda da tempo).
    """
    pessoas = list(responsavel_por_pessoa)
    entradas = MensagemContato.objects.filter(
        pessoa_id__in=pessoas,
        direcao=MensagemContato.DirecaoChoices.ENTRADA,
        enfileirada_em__gte=inicio,
    ).values_list('pessoa_id', 'enfileirada_em')
    respostas = respostas_da_equipe().filter(
        pessoa_id__in=pessoas, enfileirada_em__gte=inicio,
    ).values_list('pessoa_id', 'enfileirada_em')

    linha_do_tempo = defaultdict(list)
    for pessoa_id, quando in entradas:
        linha_do_tempo[pessoa_id].append((quando, 'entrada'))
    for pessoa_id, quando in respostas:
        linha_do_tempo[pessoa_id].append((quando, 'resposta'))

    resultado = defaultdict(lambda: ([], 0, 0))
    for pessoa_id, eventos in linha_do_tempo.items():
        tempos, pendencias, em_24h = resultado[responsavel_por_pessoa[pessoa_id]]
        esperando_desde = None
        for quando, tipo in sorted(eventos):
            if tipo == 'entrada':
                esperando_desde = esperando_desde or quando
            elif esperando_desde is not None:
                espera = quando - esperando_desde
                tempos.append(espera)
                pendencias += 1
                em_24h += espera <= JANELA
                esperando_desde = None
        if esperando_desde is not None and agora - esperando_desde > JANELA:
            pendencias += 1
        resultado[responsavel_por_pessoa[pessoa_id]] = (tempos, pendencias, em_24h)
    return resultado


def resumo(dias: int = PERIODO_PADRAO, agora=None) -> dict:
    agora = agora or timezone.now()
    inicio = agora - timedelta(days=dias)
    por_responsavel = defaultdict(_linha_vazia)
    responsavel_por_pessoa = {}

    for pessoa in _pessoas_em_acolhimento():
        linha = por_responsavel[pessoa.responsavel_atual_id]
        responsavel_por_pessoa[pessoa.pk] = pessoa.responsavel_atual_id
        linha['acolhidos'] += 1
        if atencao.atrasada(pessoa, agora):
            linha['atrasados'] += 1
        aguardando = pessoa.ultima_entrada and (
            pessoa.ultima_resposta is None or pessoa.ultima_entrada > pessoa.ultima_resposta
        )
        if aguardando:
            linha['aguardando'] += 1
            desde = pessoa.aguardando_desde or pessoa.ultima_entrada
            if agora - desde > JANELA:
                linha['aguardando_24h'] += 1
            if linha['espera_mais_antiga'] is None or desde < linha['espera_mais_antiga']:
                linha['espera_mais_antiga'] = desde

    for responsavel_id, (tempos, pendencias, em_24h) in _tempos_de_resposta(
        responsavel_por_pessoa, inicio, agora,
    ).items():
        linha = por_responsavel[responsavel_id]
        linha['tempos'], linha['pendencias'], linha['em_24h'] = tempos, pendencias, em_24h

    no_rodizio = {usuario.pk for usuario in rodizio.equipe()}
    usuarios = get_user_model().objects.filter(
        pk__in=[pk for pk in por_responsavel if pk is not None] + list(no_rodizio),
    ).select_related('perfil')

    linhas = []
    for usuario in usuarios:
        linha = _finalizar(por_responsavel[usuario.pk], agora)
        perfil = getattr(usuario, 'perfil', None)
        linha.update({
            'usuario': usuario,
            'nome': usuario.get_full_name() or usuario.get_username(),
            'no_rodizio': usuario.pk in no_rodizio,
            'tem_telefone': bool(perfil and perfil.telefone),
            'ultimo_acesso': usuario.last_login,
            'ativo': usuario.is_active,
        })
        linhas.append(linha)
    # Quem tem gente esperando ha mais tempo vem primeiro.
    linhas.sort(key=lambda l: (-l['aguardando_24h'], -l['aguardando'], -l['atrasados'], l['nome'].lower()))

    sem_responsavel = _finalizar(por_responsavel[None], agora) if None in por_responsavel else None
    totais = _finalizar(_somar(por_responsavel.values()), agora)
    return {
        'dias': dias,
        'linhas': linhas,
        'sem_responsavel': sem_responsavel,
        'totais': totais,
    }


def _somar(linhas):
    total = _linha_vazia()
    for linha in linhas:
        for chave in ('acolhidos', 'atrasados', 'aguardando', 'aguardando_24h', 'pendencias', 'em_24h'):
            total[chave] += linha[chave]
        total['tempos'] = total['tempos'] + linha['tempos']
        antiga = linha['espera_mais_antiga']
        if antiga and (total['espera_mais_antiga'] is None or antiga < total['espera_mais_antiga']):
            total['espera_mais_antiga'] = antiga
    return total


def _finalizar(linha, agora):
    tempos = linha['tempos']
    pendencias = linha['pendencias']
    return {
        **{chave: valor for chave, valor in linha.items() if chave != 'tempos'},
        'respondidas': len(tempos),
        'mediana_resposta': median(tempos) if tempos else None,
        'pct_24h': round(100 * linha['em_24h'] / pendencias) if pendencias else None,
        'mediana_texto': duracao_curta(median(tempos)) if tempos else '—',
        'espera_texto': duracao_curta(agora - linha['espera_mais_antiga']) if linha['espera_mais_antiga'] else '',
    }


def duracao_curta(valor) -> str:
    """timedelta -> '12 min', '3 h', '2 dias'."""
    if valor is None:
        return '—'
    minutos = int(valor.total_seconds() // 60)
    if minutos < 60:
        return f'{max(minutos, 1)} min'
    horas = minutos // 60
    if horas < 48:
        return f'{horas} h'
    return f'{horas // 24} dias'
