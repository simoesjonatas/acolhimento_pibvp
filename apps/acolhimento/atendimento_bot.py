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

import re

from django.utils import timezone

from apps.acolhimento.template_config import usando_meta
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
    PrimeiroContato.StatusAcolhimento.APROVADO,
    PrimeiroContato.StatusAcolhimento.MEMBRO,
}


def opcoes_ativas() -> list[OpcaoAtendimentoBot]:
    """Opcoes a que o bot responde (no menu ou so por palavra-chave), na ordem do menu."""
    return list(OpcaoAtendimentoBot.objects.filter(ativa=True).order_by('ordem', 'id'))


def opcoes_do_menu(opcoes=None) -> list[OpcaoAtendimentoBot]:
    """As opcoes ativas que aparecem (numeradas) no menu."""
    opcoes = opcoes if opcoes is not None else opcoes_ativas()
    return [op for op in opcoes if op.ativa and op.aparece_no_menu]


def montar_menu(opcoes=None) -> str:
    """Texto do menu numerado (1..N) com as opcoes ativas que aparecem no menu."""
    visiveis = opcoes_do_menu(opcoes)
    return '\n'.join(f'{i}. {op.rotulo}' for i, op in enumerate(visiveis, start=1))


def _com_menu(texto: str, opcoes) -> str:
    menu = montar_menu(opcoes)
    return f'{texto}\n\n{menu}' if menu else texto


def encontrar_opcao(texto: str, opcoes=None):
    """Casa o texto da pessoa com uma opcao.

    Numero (1..N): so as opcoes que aparecem no menu, na mesma numeracao mostrada.
    Palavra-chave: qualquer opcao ativa, inclusive as que estao fora do menu.
    """
    opcoes = opcoes if opcoes is not None else opcoes_ativas()
    if not opcoes:
        return None
    t = (texto or '').strip().lower()
    if not t:
        return None

    if t.isdigit():
        visiveis = opcoes_do_menu(opcoes)
        idx = int(t)
        return visiveis[idx - 1] if 1 <= idx <= len(visiveis) else None

    # Palavras sem pontuacao: "qual o pix?" precisa casar com a palavra-chave "pix".
    palavras = set(re.findall(r'\w+', t))
    for op in opcoes:
        for kw in op.palavras_chave_lista():
            # igualdade, palavra inteira, ou frase (multi-palavra) contida no texto.
            if kw == t or kw in palavras or (' ' in kw and kw in t):
                return op
    return None


def enfileirar_resposta(pessoa, texto: str, *, origem: str = 'bot_atendimento', extra: dict | None = None):
    """Enfileira uma mensagem de saida (o envio real fica a cargo do fila_auto)."""
    if not (texto or '').strip():
        return None
    metadata = {'origem': origem}
    if extra:
        metadata.update(extra)
    return MensagemContato.objects.create(
        pessoa=pessoa,
        canal=MensagemContato.CanalChoices.WHATSAPP,
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        status_fila=MensagemContato.StatusFilaChoices.PENDENTE,
        prioridade=5,
        conteudo=texto,
        metadata_envio=metadata,
    )


def _log(pessoa, descricao: str, agora=None):
    agora = agora or timezone.now()
    InteracaoAcolhimento.objects.create(
        pessoa=pessoa,
        tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
        descricao=descricao,
        data_interacao=agora.date(),
    )


def bot_pode_atender(pessoa, cfg=None) -> bool:
    """Bot ligado, com opcoes, e a pessoa ainda nao esta com a equipe."""
    cfg = cfg or ConfiguracaoAtendimentoBot.carregar()
    return bool(cfg.ativo and opcoes_ativas() and pessoa.status not in _STATUS_NAO_INTERVIR)


def _eh_audio_para_transcrever(mensagem, cfg) -> bool:
    return bool(
        mensagem is not None
        and cfg.transcrever_audios
        and mensagem.direcao == MensagemContato.DirecaoChoices.ENTRADA
        and mensagem.midia_tipo == MensagemContato.MidiaTipoChoices.AUDIO
    )


def reenviar_menu(pessoa, cfg, opcoes, texto: str, agora=None) -> None:
    """Comportamento de sempre quando nada casa: "nao entendi" + menu."""
    enfileirar_resposta(pessoa, _com_menu(cfg.mensagem_fallback, opcoes))
    _log(pessoa, f'Atendimento automatico: nao entendeu "{(texto or "").strip()[:60]}", reenviou o menu.', agora)


def transferir_para_equipe(pessoa, motivo: str, agora=None) -> None:
    """Tira a pessoa do bot: daqui em diante a conversa e com a equipe."""
    pessoa.status = PrimeiroContato.StatusAcolhimento.EM_ACOMPANHAMENTO
    pessoa.bot_etapa = PrimeiroContato.BotEtapaChoices.INATIVO
    pessoa.save(update_fields=['status', 'bot_etapa', 'atualizado_em'])
    _log(pessoa, f'Atendimento automatico: {motivo} -> transferido para atendente.', agora)


def aplicar_opcao(pessoa, opcao, agora=None, *, origem: str = 'bot_atendimento') -> None:
    """Responde a opcao (texto + arquivo) e, se for o caso, transfere para a equipe."""
    enfileirar_resposta(pessoa, opcao.resposta, origem=origem)
    # Arquivo da opcao (imagem/PDF/audio/video): so pela API oficial, logo apos o texto.
    if opcao.midia_arquivo and usando_meta():
        from apps.acolhimento import bot_midia

        bot_midia.enfileirar_midia(pessoa, opcao)
    if opcao.acao == OpcaoAtendimentoBot.Acao.TRANSFERIR_HUMANO:
        transferir_para_equipe(pessoa, f'opcao "{opcao.rotulo}"', agora)
    else:
        _log(pessoa, f'Atendimento automatico: respondeu a opcao "{opcao.rotulo}".', agora)


def processar_entrada(pessoa, texto: str, mensagem=None, *, em_segundo_plano: bool = False) -> bool:
    """Processa uma mensagem recebida. Retorna True se o bot assumiu o atendimento.

    Quando retorna False, o webhook mantem o comportamento legado (flip para
    Em acompanhamento na primeira resposta).

    `mensagem` e a entrada gravada pelo webhook (para audio e IA). Com
    `em_segundo_plano=True` (thread da transcricao), a IA roda aqui mesmo.
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

    from apps.acolhimento import atendimento_ia

    usa_ia = cfg.ia_ativa and mensagem is not None and atendimento_ia.ia_configurada()
    audio_pendente = not em_segundo_plano and _eh_audio_para_transcrever(mensagem, cfg)
    metadata_entrada = dict(mensagem.metadata_resposta or {}) if mensagem is not None else {}
    primeira = bool(metadata_entrada.get('bot_primeira'))

    # Primeira interacao. Sem IA, ou quando e so um cumprimento ("oi", "bom dia"):
    # saudacao fixa + menu (rapido e sem custo). Com IA e uma mensagem de verdade
    # (ex.: resposta ao template "como foi sua experiencia?"), a IA responde ao que a
    # pessoa disse e o menu vai junto.
    if pessoa.bot_etapa == Etapa.INATIVO:
        pessoa.bot_etapa = Etapa.MENU
        pessoa.status = PrimeiroContato.StatusAcolhimento.ROBO
        pessoa.save(update_fields=['bot_etapa', 'status', 'atualizado_em'])
        if not usa_ia or (not audio_pendente and eh_saudacao_simples(texto)):
            enfileirar_resposta(pessoa, _com_menu(cfg.mensagem_saudacao, opcoes))
            _log(pessoa, 'Atendimento automatico: enviou saudacao + menu.', agora)
            return True
        primeira = True
        if not audio_pendente:
            _log(pessoa, 'Atendimento automatico: primeira mensagem encaminhada para a IA.', agora)
            _chamar_ia(pessoa, mensagem, em_segundo_plano, primeira=True)
            return True

    # Audio: so da para entender depois da transcricao (feita apos o download).
    if audio_pendente:
        metadata_entrada['bot_aguardando_transcricao'] = True
        if primeira:
            metadata_entrada['bot_primeira'] = True
        mensagem.metadata_resposta = metadata_entrada
        mensagem.save(update_fields=['metadata_resposta', 'atualizado_em'])
        return True

    # Aguardando escolha no menu. Numero e mensagem curta casam direto com a opcao;
    # frase mais longa vai para a IA (com IA ligada), que conhece as opcoes e pode
    # escolher uma delas ou responder o resto ("que horas e o culto e tem estacionamento?").
    opcao = encontrar_opcao(texto, opcoes)
    if opcao is not None and not (usa_ia and _frase_longa(texto)):
        aplicar_opcao(pessoa, opcao, agora)
        return True

    if usa_ia:
        _chamar_ia(pessoa, mensagem, em_segundo_plano, primeira=primeira)
        return True

    if primeira:
        # Primeiro audio que nao deu para transcrever: saudacao + menu, nao "nao entendi".
        enfileirar_resposta(pessoa, _com_menu(cfg.mensagem_saudacao, opcoes))
        _log(pessoa, 'Atendimento automatico: enviou saudacao + menu.', agora)
        return True

    reenviar_menu(pessoa, cfg, opcoes, texto, agora)
    return True


def _chamar_ia(pessoa, mensagem, em_segundo_plano: bool, *, primeira: bool) -> None:
    from apps.acolhimento import atendimento_ia

    if em_segundo_plano:
        atendimento_ia.responder(pessoa.pk, mensagem.pk, primeira=primeira)
    else:
        atendimento_ia.agendar(pessoa.pk, mensagem.pk, primeira=primeira)


# Palavras de um cumprimento sem pedido: com elas a 1a mensagem recebe a saudacao fixa.
_PALAVRAS_SAUDACAO = {
    'oi', 'oie', 'oii', 'ola', 'olá', 'opa', 'eai', 'eae', 'e', 'ai', 'aí', 'bom', 'boa', 'dia', 'tarde',
    'noite', 'tudo', 'bem', 'td', 'blz', 'beleza', 'paz', 'graça', 'graca', 'a', 'hello', 'hi', 'salve',
    'amém', 'amem', 'irmão', 'irmao', 'irmã', 'irma', 'gente', 'pessoal', 'como', 'vai', 'vão', 'vao',
}


def eh_saudacao_simples(texto: str) -> bool:
    """"oi", "bom dia!", "olá, tudo bem?", figurinha/midia sem texto: nada para a IA entender."""
    t = (texto or '').strip().lower()
    if not t or (t.startswith('(') and t.endswith(')')):
        return True
    palavras = re.findall(r'\w+', t)
    return len(palavras) <= 5 and all(p in _PALAVRAS_SAUDACAO for p in palavras)


PALAVRAS_FRASE_LONGA = 4


def _frase_longa(texto: str) -> bool:
    texto = (texto or '').strip()
    return not texto.isdigit() and len(re.findall(r'\w+', texto)) > PALAVRAS_FRASE_LONGA
