"""Numero que o WhatsApp nao entrega (erro #131026 da Meta).

A Meta devolve "Message undeliverable" quando o numero nao tem WhatsApp, esta
digitado errado, bloqueou o numero da igreja ou nao aceitou os termos atuais do
WhatsApp. A ficha, a conversa e a lista avisam a equipe para conferir o telefone.

O aviso some sozinho quando o numero mostra que funciona (a pessoa escreveu ou
uma mensagem foi entregue depois da falha) ou quando o telefone e corrigido no
cadastro.
"""
from __future__ import annotations

from django.utils import timezone

from apps.acolhimento.models import MensagemContato

CODIGO_NAO_ENTREGUE = '#131026'
CHAVE_CORRIGIDO = 'numero_corrigido_em'

_ROTULOS = {
    'primeiro_contato_opt_in': 'a mensagem de boas-vindas',
    'continuar_conversa': 'o convite para continuar a conversa',
    'marketing_massa': 'o disparo em massa',
    'gabinete_agendado_pessoa': 'a confirmação do gabinete',
}


def _falhas(pessoa_ids):
    return (
        MensagemContato.objects.filter(
            pessoa_id__in=pessoa_ids,
            direcao=MensagemContato.DirecaoChoices.SAIDA,
            status_fila=MensagemContato.StatusFilaChoices.FALHA,
            erro_ultimo_envio__contains=CODIGO_NAO_ENTREGUE,
        )
        .exclude(metadata_envio__has_key=CHAVE_CORRIGIDO)
        .order_by('-atualizado_em')
    )


def _funcionou_depois(pessoa_id, quando) -> bool:
    """Ela escreveu, ou uma mensagem nossa foi entregue, depois da falha."""
    mensagens = MensagemContato.objects.filter(pessoa_id=pessoa_id)
    escreveu = mensagens.filter(direcao=MensagemContato.DirecaoChoices.ENTRADA, enfileirada_em__gt=quando)
    entregue = mensagens.filter(direcao=MensagemContato.DirecaoChoices.SAIDA, entregue_em__gt=quando)
    return escreveu.exists() or entregue.exists()


def falhas_por_pessoa(pessoa_ids) -> dict[int, MensagemContato]:
    """{pessoa_id: ultima falha de numero ainda valida} para as pessoas informadas."""
    ultimas = {}
    for falha in _falhas(list(pessoa_ids)):
        ultimas.setdefault(falha.pessoa_id, falha)
    return {pid: f for pid, f in ultimas.items() if not _funcionou_depois(pid, f.atualizado_em)}


def falha_do_numero(pessoa):
    """A ultima mensagem que nao chegou por causa do numero, ou None."""
    falha = falhas_por_pessoa([pessoa.pk]).get(pessoa.pk)
    if falha is not None:
        tipo = (falha.metadata_envio or {}).get('tipo_template') or ''
        falha.rotulo_tipo = _ROTULOS.get(tipo, 'a mensagem')
    return falha


def marcar_numero_corrigido(pessoa) -> int:
    """Telefone trocado no cadastro: as falhas antigas deixam de valer para o aviso."""
    agora = timezone.now().isoformat()
    total = 0
    for falha in _falhas([pessoa.pk]):
        metadata = dict(falha.metadata_envio or {})
        metadata[CHAVE_CORRIGIDO] = agora
        MensagemContato.objects.filter(pk=falha.pk).update(metadata_envio=metadata)
        total += 1
    return total
