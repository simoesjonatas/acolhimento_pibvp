"""Quem ve quais pessoas.

- Administradores (staff/superuser) e pastores (perfil `pode_ver_gabinete`) veem a base toda.
- Os demais usuarios veem so os proprios acolhidos: as pessoas em que sao o
  responsavel atual. Quem cadastra uma pessoa vira o responsavel dela.

Toda tela que mostra pessoas, conversas, passos ou questionarios passa por aqui. Uma
pessoa fora do alcance responde 404 (nem confirma que ela existe).
"""
from __future__ import annotations

from apps.acolhimento.models import PrimeiroContato

PERMISSAO_GABINETE = 'acolhimento.pode_ver_gabinete'


def ve_toda_base(user) -> bool:
    return bool(user.is_staff or user.is_superuser or user.has_perm(PERMISSAO_GABINETE))


def pessoas_visiveis(user, queryset=None):
    queryset = PrimeiroContato.objects.all() if queryset is None else queryset
    if ve_toda_base(user):
        return queryset
    return queryset.filter(responsavel_atual=user)


def filtrar_por_pessoa(queryset, user, campo: str = 'pessoa'):
    """Restringe um queryset ligado a PrimeiroContato (mensagens, passos, convites...)."""
    if ve_toda_base(user):
        return queryset
    return queryset.filter(**{f'{campo}__responsavel_atual': user})
