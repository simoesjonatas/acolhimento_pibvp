"""Motor do atendimento automatico (bot de menu) no WhatsApp.

Fluxo: a pessoa manda mensagem (webhook do Evolution) -> `processar_entrada`
decide o que responder. Primeira mensagem = saudacao + menu numerado. Depois,
casa a resposta por numero ou palavra-chave e executa a acao da opcao
(so responder, ou transferir para atendente humano).

As respostas sao apenas ENFILEIRADAS (MensagemContato SAIDA/PENDENTE). O envio
real acontece pela cadeia existente (signal post_save -> fila_auto), ou seja,
so sai se o "Processamento automatico" (ConfiguracaoProcessamentoFila) estiver ligado.
Nao envia nada diretamente aqui.
"""
from __future__ import annotations

from django.utils import timezone

from apps.acolhimento.models import (
    ConfiguracaoAtendimentoBot,
    InteracaoAcolhimento,
    MensagemContato,
    OpcaoAtendimentoBot,
    PrimeiroContato,
)

# Status em que ja existe um humano/relacionamento: o bot nao intervem.
_STATUS_NAO_INTERVIR = {
    PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO,
    PrimeiroContato.StatusAcolhimento.PARTICIPANTE,
    PrimeiroContato.StatusAcolhimento.MEMBRO,
}


def opcoes_ativas() -> list[OpcaoAtendimentoBot]:
    return list(OpcaoAtendimentoBot.objects.filter(ativa=True).order_by('ordem', 'id'))


def montar_menu(opcoes=None) -> str:
    """Texto do menu numerado (1..N) a partir dos rotulos das opcoes ativas."""
    opcoes = opcoes if opcoes is not None else opcoes_ativas()
    return '\n'.join(f'{i}. {op.rotulo}' for i, op in enumerate(opcoes, start=1))


def encontrar_opcao(texto: str, opcoes=None):
    """Casa o texto da pessoa com uma opcao: por numero (1..N) ou por palavra-chave."""
    opcoes = opcoes if opcoes is not None else opcoes_ativas()
    if not opcoes:
        return None
    t = (texto or '').strip().lower()
    if not t:
        return None

    if t.isdigit():
        idx = int(t)
        return opcoes[idx - 1] if 1 <= idx <= len(opcoes) else None

    palavras = set(t.split())
    for op in opcoes:
        for kw in op.palavras_chave_lista():
            # igualdade, palavra inteira, ou frase (multi-palavra) contida no texto.
            if kw == t or kw in palavras or (' ' in kw and kw in t):
                return op
    return None


def enfileirar_resposta(pessoa, texto: str):
    """Enfileira uma mensagem de saida (o envio real fica a cargo do fila_auto)."""
    if not (texto or '').strip():
        return None
    return MensagemContato.objects.create(
        pessoa=pessoa,
        canal=MensagemContato.CanalChoices.WHATSAPP,
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
        prioridade=5,
        conteudo=texto,
        metadata_envio={'origem': 'bot_atendimento'},
    )


def _log(pessoa, descricao: str, agora=None):
    agora = agora or timezone.now()
    InteracaoAcolhimento.objects.create(
        pessoa=pessoa,
        tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
        descricao=descricao,
        data_interacao=agora.date(),
    )


def processar_entrada(pessoa, texto: str) -> bool:
    """Processa uma mensagem recebida. Retorna True se o bot assumiu o atendimento.

    Quando retorna False, o webhook mantem o comportamento legado (flip para
    Em acompanhamento na primeira resposta).
    """
    cfg = ConfiguracaoAtendimentoBot.carregar()
    if not cfg.ativo:
        return False

    opcoes = opcoes_ativas()
    if not opcoes:
        return False

    if pessoa.status in _STATUS_NAO_INTERVIR:
        return False

    agora = timezone.now()
    Etapa = PrimeiroContato.BotEtapaChoices

    # Primeira interacao: cumprimenta e mostra o menu (nao interpreta o texto inicial).
    if pessoa.bot_etapa == Etapa.INATIVO:
        enfileirar_resposta(pessoa, f'{cfg.mensagem_saudacao}\n\n{montar_menu(opcoes)}')
        pessoa.bot_etapa = Etapa.MENU
        pessoa.status = PrimeiroContato.StatusAcolhimento.ROBO
        pessoa.save(update_fields=['bot_etapa', 'status', 'atualizado_em'])
        _log(pessoa, 'Atendimento automatico: enviou saudacao + menu.', agora)
        return True

    # Aguardando escolha no menu.
    opcao = encontrar_opcao(texto, opcoes)
    if opcao is None:
        enfileirar_resposta(pessoa, f'{cfg.mensagem_fallback}\n\n{montar_menu(opcoes)}')
        _log(pessoa, f'Atendimento automatico: nao entendeu "{(texto or "").strip()[:60]}", reenviou o menu.', agora)
        return True

    enfileirar_resposta(pessoa, opcao.resposta)
    if opcao.acao == OpcaoAtendimentoBot.Acao.TRANSFERIR_HUMANO:
        pessoa.status = PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
        pessoa.bot_etapa = Etapa.INATIVO
        pessoa.save(update_fields=['status', 'bot_etapa', 'atualizado_em'])
        _log(pessoa, f'Atendimento automatico: opcao "{opcao.rotulo}" -> transferido para atendente.', agora)
    else:
        _log(pessoa, f'Atendimento automatico: respondeu a opcao "{opcao.rotulo}".', agora)
    return True
