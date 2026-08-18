"""Variacao automatica das mensagens de boas-vindas (anti-bloqueio).

O WhatsApp bloqueia contas que disparam a MESMA mensagem, byte a byte, para
varias pessoas. Para evitar isso, geramos uma variacao leve e DETERMINISTICA por
mensagem (seed = id da mensagem): troca a saudacao inicial (inclusive por periodo
do dia), ajusta a pontuacao e, as vezes, adiciona um emoji ao final.

Principios:
  - Nao muda o sentido do texto configurado pela equipe (so mexe na "casca").
  - Deterministico por seed: reenviar a mesma mensagem gera o mesmo texto.
  - Sem truques sujos (nada de caracteres invisiveis / zero-width, que pioram o
    score de spam em vez de ajudar).
"""
from __future__ import annotations

import random
import re

from django.utils import timezone


# Saudacoes neutras intercambiaveis (o nome vem logo depois).
_SAUDACOES_NEUTRAS = ['Oi', 'Ola', 'Ola', 'Oie', 'Opa']

# Saudacoes por periodo do dia (dao um tom mais humano e variam ao longo do dia).
_SAUDACOES_MANHA = ['Bom dia', 'Bom dia', 'Oi, bom dia']
_SAUDACOES_TARDE = ['Boa tarde', 'Boa tarde', 'Oi, boa tarde']
_SAUDACOES_NOITE = ['Boa noite', 'Boa noite', 'Oi, boa noite']

# Emojis ocasionais ao final (string vazia = sem emoji).
_EMOJIS = ['\U0001F642', '\U0001F60A', '\U0001F64F', '', '', '']
_EMOJIS_SET = {e for e in _EMOJIS if e}

# Separador entre a saudacao e o nome (maioria espaco simples; as vezes virgula).
_SEPARADORES = [' ', ' ', ' ', ', ']

# Saudacao no inicio do texto base. Consome tambem a pontuacao/espaco logo apos,
# para podermos trocar so a saudacao mantendo o restante ("{nome}! ...") intacto.
_SAUDACAO_INICIAL_RE = re.compile(
    r'^\s*(bom\s+dia|boa\s+tarde|boa\s+noite|ol[aá]|oi+e?|opa|ei)\b[\s,!\.]*',
    re.IGNORECASE,
)


def saudacao_por_horario(hora: int, rng: random.Random) -> str:
    """Escolhe uma saudacao coerente com a hora (0-23), com um pouco de variacao."""
    if 5 <= hora < 12:
        pool = _SAUDACOES_MANHA
    elif 12 <= hora < 18:
        pool = _SAUDACOES_TARDE
    else:
        pool = _SAUDACOES_NOITE
    # Parte das vezes usa saudacao neutra, para nao ficar sempre igual no mesmo turno.
    if rng.random() < 0.4:
        return rng.choice(_SAUDACOES_NEUTRAS)
    return rng.choice(pool)


def _trocar_saudacao(texto: str, rng: random.Random, hora: int) -> str:
    match = _SAUDACAO_INICIAL_RE.match(texto)
    if not match:
        # Sem saudacao reconhecida no inicio: nao injeta nada (evita mangear texto livre).
        return texto
    resto = texto[match.end():]
    nova = saudacao_por_horario(hora, rng)
    separador = rng.choice(_SEPARADORES)
    return f'{nova}{separador}{resto}'


def _ajustar_emoji(texto: str, rng: random.Random) -> str:
    texto = texto.rstrip()
    if not texto:
        return texto
    if texto[-1] in _EMOJIS_SET:
        # Ja termina em emoji: nao duplica.
        return texto
    emoji = rng.choice(_EMOJIS)
    if not emoji:
        return texto
    return f'{texto} {emoji}'


def variar_texto(
    texto_base: str,
    nome: str = '',
    *,
    seed=None,
    hora: int | None = None,
) -> str:
    """Gera uma variacao leve e deterministica (por `seed`) do texto de boas-vindas.

    - `nome`: se informado, substitui `{nome}` no texto (fallback 'amigo(a)').
    - `seed`: use o id da mensagem para variacao estavel e distinta por pessoa.
    - `hora`: hora local (0-23) para a saudacao; se None, usa a hora atual.
    """
    texto = (texto_base or '').strip()
    nome_final = (nome or '').strip() or 'amigo(a)'
    texto = texto.replace('{nome}', nome_final)

    rng = random.Random(seed)
    if hora is None:
        hora = timezone.localtime().hour

    texto = _trocar_saudacao(texto, rng, hora)
    texto = _ajustar_emoji(texto, rng)
    return texto
