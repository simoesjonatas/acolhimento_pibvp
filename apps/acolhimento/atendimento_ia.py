"""IA do robo: entende texto livre (ou audio transcrito) e responde com limites.

Entra quando o texto nao casa com numero nem palavra-chave do menu. A IA so pode:
  - responder usando a base de conhecimento e as respostas das opcoes;
  - escolher uma opcao do menu (o bot envia a resposta dela, inclusive arquivo);
  - transferir para a equipe (assunto pastoral/sensivel, ou quando nao sabe).
Qualquer falha (sem chave, erro, 429, timeout, resposta ilegivel) cai no
comportamento de sempre: "nao entendi" + menu. A pessoa nunca fica sem resposta.

Roda fora do webhook (thread apos o commit) porque a Ollama pode levar segundos.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.acolhimento import atendimento_bot, ia_service
from apps.acolhimento.models import ConfiguracaoAtendimentoBot, MensagemContato, PrimeiroContato

logger = logging.getLogger(__name__)

ORIGEM_IA = 'bot_ia'
# Espera antes de responder: se a pessoa mandar outra mensagem, so a ultima responde.
ESPERA_DEBOUNCE_SEGUNDOS = 4
HISTORICO_MENSAGENS = 10
MENSAGEM_TRANSFERENCIA = 'Vou chamar alguem da nossa equipe para continuar a conversa com voce. Ja ja alguem te responde!'

ACOES = ('responder', 'opcao', 'transferir')

REGRAS = """Voce e o assistente de WhatsApp do Ministerio de Acolhimento de uma igreja batista.
Fale em portugues do Brasil, com carinho, de forma curta (no maximo 3 frases) e sem emojis em excesso.

Regras obrigatorias:
1. Responda SOMENTE com informacoes da BASE DE CONHECIMENTO e das OPCOES abaixo. Nunca invente horarios, enderecos, valores, nomes ou eventos.
2. Se a pergunta corresponde a uma opcao, use acao "opcao" com o numero dela (a resposta oficial da opcao sera enviada).
3. Use acao "transferir" quando: voce nao souber a resposta; a pessoa pedir para falar com alguem; houver luto, doenca, crise, sofrimento, pedido de oracao, pedido de visita ou aconselhamento; reclamacao; ou qualquer assunto pastoral, medico, juridico ou financeiro pessoal.
4. Nunca de conselhos pastorais, medicos ou juridicos, nunca faca promessas e nunca peca dados pessoais.
5. Cumprimentos e agradecimentos simples: responda com gentileza e, se fizer sentido, lembre que pode ajudar com as opcoes.

Responda APENAS com um JSON, sem texto antes ou depois, neste formato:
{"acao": "responder" | "opcao" | "transferir", "opcao": <numero da opcao ou null>, "resposta": "<texto para a pessoa>"}"""


# ----------------------------------------------------------------- configuracao
def ia_configurada() -> bool:
    return ia_service.ollama_configurada()


def _modelo(cfg) -> str:
    return (cfg.ia_modelo or '').strip() or ia_service.modelo_padrao()


# ----------------------------------------------------------------------- prompt
def _texto_da_mensagem(mensagem: MensagemContato) -> str:
    if mensagem.transcricao:
        return f'(audio) {mensagem.transcricao}'
    return (mensagem.conteudo or '').strip()


PRIMEIRA_RESPOSTA = (
    'ESTA E A PRIMEIRA RESPOSTA do atendimento a esta pessoa. Se a mensagem dela responde a '
    'uma mensagem anterior da igreja (por exemplo, "como foi sua experiencia?"), agradeca e reaja '
    'com carinho ao que ela disse; se ela fez uma pergunta, responda. Nao liste as opcoes: o menu '
    'sera enviado logo depois da sua resposta.'
)


def montar_prompt(
    cfg, opcoes, historico: list[tuple[str, str]], nome: str = '', *, primeira: bool = False,
) -> list[dict[str, str]]:
    """Mensagens para o chat: regras + base + opcoes no system; conversa como user/assistant."""
    linhas_opcoes = []
    for numero, opcao in enumerate(opcoes, start=1):
        tipo = 'transfere para a equipe' if opcao.acao == opcao.Acao.TRANSFERIR_HUMANO else 'responde'
        resposta = (opcao.resposta or '').strip().replace('\n', ' ')
        linhas_opcoes.append(f'{numero}. {opcao.rotulo} ({tipo}): {resposta[:400]}')
    system = REGRAS
    if (cfg.ia_instrucoes or '').strip():
        system += f'\n\nINSTRUCOES DA IGREJA:\n{cfg.ia_instrucoes.strip()}'
    system += '\n\nBASE DE CONHECIMENTO:\n' + ((cfg.ia_base_conhecimento or '').strip() or '(vazia)')
    system += '\n\nOPCOES:\n' + ('\n'.join(linhas_opcoes) or '(nenhuma)')
    if nome:
        system += f'\n\nPrimeiro nome da pessoa: {nome}'
    if primeira:
        system += f'\n\n{PRIMEIRA_RESPOSTA}'

    mensagens = [{'role': 'system', 'content': system}]
    for papel, texto in historico:
        if texto:
            mensagens.append({'role': papel, 'content': texto[:1500]})
    return mensagens


def _historico(pessoa) -> list[tuple[str, str]]:
    recentes = list(
        MensagemContato.objects.filter(pessoa=pessoa, canal=MensagemContato.CanalChoices.WHATSAPP)
        .exclude(status_fila__in=[MensagemContato.StatusFilaChoices.FALHA, MensagemContato.StatusFilaChoices.CANCELADA])
        .order_by('-enfileirada_em', '-id')[:HISTORICO_MENSAGENS]
    )
    recentes.reverse()
    return [
        ('user' if m.direcao == MensagemContato.DirecaoChoices.ENTRADA else 'assistant', _texto_da_mensagem(m))
        for m in recentes
    ]


def _primeiro_nome(pessoa) -> str:
    partes = (pessoa.nome or '').strip().split()
    primeiro = partes[0] if partes else ''
    # Nome automatico ("Contato 2199...") nao e nome de gente.
    return '' if not primeiro or any(c.isdigit() for c in primeiro) or primeiro.lower() == 'contato' else primeiro


# ------------------------------------------------------------------ resposta IA
def interpretar_resposta(texto: str, total_opcoes: int) -> dict | None:
    """Le o JSON da IA de forma tolerante. None = ilegivel (o bot cai no menu)."""
    texto = (texto or '').strip()
    # Tira cercas de codigo (```json ... ```) e pega do primeiro { ao ultimo }.
    texto = re.sub(r'^```(?:json)?|```$', '', texto, flags=re.IGNORECASE).strip()
    inicio, fim = texto.find('{'), texto.rfind('}')
    if inicio < 0 or fim <= inicio:
        return None
    try:
        dados = json.loads(texto[inicio:fim + 1])
    except ValueError:
        return None
    if not isinstance(dados, dict):
        return None
    acao = str(dados.get('acao') or '').strip().lower()
    resposta = str(dados.get('resposta') or '').strip()
    if acao not in ACOES:
        return None
    if acao == 'opcao':
        try:
            numero = int(dados.get('opcao'))
        except (TypeError, ValueError):
            return None
        if not 1 <= numero <= total_opcoes:
            return None
        return {'acao': 'opcao', 'opcao': numero, 'resposta': resposta}
    if acao == 'responder' and not resposta:
        return None
    return {'acao': acao, 'opcao': None, 'resposta': resposta[:1000]}


def consultar(cfg, opcoes, historico, nome: str = '', *, primeira: bool = False) -> dict:
    """Chama a IA e interpreta. Levanta ia_service.IAError; resposta ilegivel vira acao None."""
    resultado = ia_service.chat(
        montar_prompt(cfg, opcoes, historico, nome, primeira=primeira), modelo=_modelo(cfg),
    )
    decisao = interpretar_resposta(resultado['texto'], len(opcoes))
    return {**resultado, 'decisao': decisao}


def testar(pergunta: str) -> dict:
    """Usado pela tela de configuracao: pergunta avulsa, nada vai para o WhatsApp."""
    cfg = ConfiguracaoAtendimentoBot.carregar()
    opcoes = atendimento_bot.opcoes_ativas()
    try:
        resultado = consultar(cfg, opcoes, [('user', (pergunta or '').strip())])
    except ia_service.IAError as exc:
        return {'erro': str(exc)}
    decisao = resultado['decisao']
    if decisao and decisao['acao'] == 'opcao':
        opcao = opcoes[decisao['opcao'] - 1]
        decisao = {**decisao, 'rotulo': opcao.rotulo, 'resposta': opcao.resposta}
    return {
        'decisao': decisao,
        'bruto': resultado['texto'],
        'modelo': resultado['modelo'],
        'tokens_entrada': resultado['tokens_entrada'],
        'tokens_saida': resultado['tokens_saida'],
    }


# -------------------------------------------------------------------- execucao
def _respostas_ia_hoje(pessoa) -> int:
    desde = timezone.now() - timedelta(hours=24)
    return MensagemContato.objects.filter(
        pessoa=pessoa,
        direcao=MensagemContato.DirecaoChoices.SAIDA,
        metadata_envio__origem=ORIGEM_IA,
        enfileirada_em__gte=desde,
    ).count()


def _tem_entrada_mais_nova(pessoa, mensagem_id: int) -> bool:
    return MensagemContato.objects.filter(
        pessoa=pessoa, direcao=MensagemContato.DirecaoChoices.ENTRADA, pk__gt=mensagem_id,
    ).exists()


def _sem_ia(pessoa, cfg, opcoes, texto: str, agora, *, primeira: bool = False) -> str:
    """IA falhou: tenta a palavra-chave (frase longa pulou essa etapa) e, sem ela, o menu."""
    if primeira:
        # Primeira mensagem: o mesmo de antes da IA (saudacao + menu), nao "nao entendi".
        atendimento_bot.enfileirar_resposta(pessoa, atendimento_bot._com_menu(cfg.mensagem_saudacao, opcoes))
        atendimento_bot._log(pessoa, 'Atendimento automatico: IA indisponivel, enviou saudacao + menu.', agora)
        return 'fallback'
    opcao = atendimento_bot.encontrar_opcao(texto, opcoes)
    if opcao is not None:
        atendimento_bot.aplicar_opcao(pessoa, opcao, agora)
        return 'fallback_opcao'
    atendimento_bot.reenviar_menu(pessoa, cfg, opcoes, texto, agora)
    return 'fallback'


def responder(pessoa_id: int, mensagem_id: int, *, esperar: bool = True, primeira: bool = False) -> str:
    """Responde a entrada com a IA. Devolve a acao tomada (para log/teste)."""
    if esperar:
        time.sleep(ESPERA_DEBOUNCE_SEGUNDOS)
    pessoa = PrimeiroContato.objects.filter(pk=pessoa_id).first()
    mensagem = MensagemContato.objects.filter(pk=mensagem_id).first()
    if not pessoa or not mensagem:
        return 'ignorado'
    cfg = ConfiguracaoAtendimentoBot.carregar()
    if not atendimento_bot.bot_pode_atender(pessoa, cfg):
        return 'ignorado'
    if _tem_entrada_mais_nova(pessoa, mensagem.pk):
        # A pessoa mandou outra mensagem: a resposta sai pela mais nova (com o historico todo).
        return 'debounce'

    opcoes = atendimento_bot.opcoes_ativas()
    agora = timezone.now()
    if _respostas_ia_hoje(pessoa) >= max(int(cfg.ia_max_respostas_por_dia or 0), 1):
        atendimento_bot.enfileirar_resposta(pessoa, MENSAGEM_TRANSFERENCIA, origem=ORIGEM_IA, extra={'ia': {'acao': 'limite'}})
        atendimento_bot.transferir_para_equipe(pessoa, 'IA atingiu o limite de respostas do dia', agora)
        return 'limite'

    texto = (mensagem.transcricao or mensagem.conteudo or '').strip()
    try:
        resultado = consultar(cfg, opcoes, _historico(pessoa), _primeiro_nome(pessoa), primeira=primeira)
    except ia_service.IAError as exc:
        logger.warning('IA do robo indisponivel (pessoa=%s, http=%s): %s', pessoa.pk, exc.http_status, exc)
        return _sem_ia(pessoa, cfg, opcoes, texto, agora, primeira=primeira)

    decisao = resultado['decisao']
    meta_ia = {
        'modelo': resultado['modelo'],
        'tokens_entrada': resultado['tokens_entrada'],
        'tokens_saida': resultado['tokens_saida'],
        'acao': decisao['acao'] if decisao else 'ilegivel',
        'entrada_id': mensagem.pk,
        'primeira': primeira,
    }
    if decisao is None:
        logger.warning('IA do robo devolveu resposta ilegivel (pessoa=%s).', pessoa.pk)
        return _sem_ia(pessoa, cfg, opcoes, texto, agora, primeira=primeira)

    if decisao['acao'] == 'opcao':
        opcao = opcoes[decisao['opcao'] - 1]
        atendimento_bot.aplicar_opcao(pessoa, opcao, agora, origem=ORIGEM_IA)
        return 'opcao'

    if decisao['acao'] == 'transferir':
        atendimento_bot.enfileirar_resposta(
            pessoa, decisao['resposta'] or MENSAGEM_TRANSFERENCIA, origem=ORIGEM_IA, extra={'ia': meta_ia},
        )
        atendimento_bot.transferir_para_equipe(pessoa, 'a IA pediu ajuda da equipe', agora)
        return 'transferir'

    resposta = decisao['resposta']
    if primeira:
        # Na primeira resposta o menu vai junto, como na saudacao fixa.
        resposta = atendimento_bot._com_menu(resposta, opcoes)
    atendimento_bot.enfileirar_resposta(pessoa, resposta, origem=ORIGEM_IA, extra={'ia': meta_ia})
    atendimento_bot._log(pessoa, f'Atendimento automatico: IA respondeu "{texto[:60]}".', agora)
    return 'responder'


def _responder_em_thread(pessoa_id: int, mensagem_id: int, primeira: bool = False) -> None:
    try:
        responder(pessoa_id, mensagem_id, primeira=primeira)
    except Exception:  # noqa: BLE001 - thread de fundo nao pode morrer com traceback solto.
        logger.exception('Erro inesperado na IA do robo (mensagem %s).', mensagem_id)
    finally:
        close_old_connections()


def agendar(pessoa_id: int, mensagem_id: int, *, primeira: bool = False) -> None:
    """Responde em segundo plano DEPOIS do commit (o webhook responde a Meta antes)."""
    transaction.on_commit(
        lambda: threading.Thread(
            target=_responder_em_thread, args=(pessoa_id, mensagem_id, primeira), daemon=True,
        ).start()
    )
