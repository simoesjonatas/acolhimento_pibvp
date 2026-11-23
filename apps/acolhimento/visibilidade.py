"""Quem ve quais pessoas.

- Veem a base toda: administradores (staff/superuser), pastores (perfil
  `pode_ver_gabinete`) e quem esta no rodizio ("Recebe novos contatos" com o perfil de
  mensagens).
- Os demais usuarios veem os proprios acolhidos (as pessoas em que sao o responsavel
  atual; quem cadastra vira o responsavel) e, ate sair do sistema, quem eles acharam
  digitando o telefone COMPLETO na busca: se a pessoa passou o numero, o voluntario
  esta com ela. Pedaco de numero nao libera ninguem (senao daria para varrer a base).

Toda tela que mostra pessoas, conversas, passos ou questionarios passa por aqui. Uma
pessoa fora do alcance responde 404 (nem confirma que ela existe).
"""
from __future__ import annotations

from django.db.models import Q

from apps.acolhimento.models import PrimeiroContato
from apps.acolhimento.phone_utils import build_phone_candidates, only_digits

PERMISSAO_GABINETE = 'acolhimento.pode_ver_gabinete'
PERMISSAO_CONVERSAR = 'acolhimento.pode_conversar_pessoas'

# Pessoas liberadas pela busca por telefone ficam na sessao (somem ao sair do sistema).
CHAVE_SESSAO = 'pessoas_liberadas_por_telefone'
LIMITE_LIBERADAS = 50
# DDD + numero: menos que isso nao e um telefone completo.
DIGITOS_TELEFONE_COMPLETO = 10


def eh_lideranca(user) -> bool:
    """Admin ou pastor: telas de lideranca (painel da equipe)."""
    return bool(user.is_staff or user.is_superuser or user.has_perm(PERMISSAO_GABINETE))


def _no_rodizio(user) -> bool:
    from apps.core.models import PerfilUsuario

    return bool(
        user.is_active
        and user.has_perm(PERMISSAO_CONVERSAR)
        and PerfilUsuario.objects.filter(usuario=user, recebe_novos_contatos=True).exists()
    )


def ve_toda_base(user) -> bool:
    if not getattr(user, 'is_authenticated', False):
        return False
    guardado = getattr(user, '_ve_toda_base', None)
    if guardado is None:
        guardado = eh_lideranca(user) or _no_rodizio(user)
        try:
            user._ve_toda_base = guardado  # varias telas perguntam na mesma requisicao
        except AttributeError:
            pass
    return guardado


def _liberadas(user) -> list[int]:
    return list(getattr(user, '_pessoas_liberadas', ()) or ())


def pessoas_visiveis(user, queryset=None):
    queryset = PrimeiroContato.objects.all() if queryset is None else queryset
    if ve_toda_base(user):
        return queryset
    return queryset.filter(Q(responsavel_atual=user) | Q(pk__in=_liberadas(user)))


def filtrar_por_pessoa(queryset, user, campo: str = 'pessoa'):
    """Restringe um queryset ligado a PrimeiroContato (mensagens, passos, convites...)."""
    if ve_toda_base(user):
        return queryset
    return queryset.filter(
        Q(**{f'{campo}__responsavel_atual': user}) | Q(**{f'{campo}_id__in': _liberadas(user)})
    )


# ---------------------------------------------------------------------------
# Busca pelo telefone
# ---------------------------------------------------------------------------
def ids_por_telefone(busca: str) -> list[int]:
    """Pessoas com exatamente esse telefone (aceita com ou sem 55, 9 e formatacao)."""
    if len(only_digits(busca)) < DIGITOS_TELEFONE_COMPLETO:
        return []
    candidatos = build_phone_candidates(busca)
    ids = []
    for pk, telefone in PrimeiroContato.objects.exclude(telefone_whatsapp='').values_list('pk', 'telefone_whatsapp'):
        digitos = only_digits(telefone)
        if digitos in candidatos or f'55{digitos}' in candidatos or (digitos.startswith('55') and digitos[2:] in candidatos):
            ids.append(pk)
    return ids


def liberar(request, ids) -> None:
    """Libera as pessoas para o usuario ate ele sair do sistema."""
    if not ids:
        return
    atuais = [pk for pk in request.session.get(CHAVE_SESSAO, []) if pk not in ids]
    novas = (list(ids) + atuais)[:LIMITE_LIBERADAS]
    request.session[CHAVE_SESSAO] = novas
    request.user._pessoas_liberadas = novas


class PessoasLiberadasMiddleware:
    """Leva para o usuario da requisicao as pessoas liberadas pela busca (na sessao)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        sessao = getattr(request, 'session', None)
        if sessao is not None and sessao.get(CHAVE_SESSAO):
            user = getattr(request, 'user', None)
            if user is not None and user.is_authenticated:
                user._pessoas_liberadas = list(sessao.get(CHAVE_SESSAO) or [])
        return self.get_response(request)
