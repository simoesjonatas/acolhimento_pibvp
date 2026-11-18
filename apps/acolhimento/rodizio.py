"""Rodizio de responsaveis: quem escreve sem ter responsavel ja cai na mao de alguem.

- Quem entra: usuarios ativos com "Recebe novos contatos" ligado no cadastro de
  usuarios (PerfilUsuario.recebe_novos_contatos) e com o perfil de mensagens.
- Quando: a pessoa sem responsavel precisa de alguem da equipe. Com o robo/IA ligado,
  so quando ele transfere (pediu para falar com alguem, assunto pastoral, a IA nao
  sabia, limite do dia); com o robo desligado (ou a pessoa ja com a equipe), na
  mensagem dela. Tambem no cadastro pelo QR code e no cadastro feito por quem nao
  atende mensagens. Reacao e o botao "Agora nao" nunca contam.
- Quem recebe: quem esta ha mais tempo sem receber ninguem (rodizio equilibrado).
- Aviso: WhatsApp no telefone do perfil do voluntario, com o nome da pessoa e o link
  da conversa. Na Meta vai pelo template RESPONSAVEL_ATRIBUIDO; sem template, so chega
  se o voluntario escreveu para o numero nas ultimas 24h.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import close_old_connections, transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone

from apps.acolhimento import evolution_service, meta_service, template_config
from apps.acolhimento.models import InteracaoAcolhimento, MensagemContato, PrimeiroContato, TemplateWhatsapp

logger = logging.getLogger(__name__)

Tipo = TemplateWhatsapp.Tipo

# A Meta recusa template que comeca ou termina com variavel: o link fica no meio.
TEXTO_AVISO_PADRAO = (
    'Olá, {voluntario}! {pessoa} mandou mensagem no WhatsApp da igreja e agora está com você. '
    'Abra a conversa em {link} e responda em até 24h.'
)
# {{1}} voluntario, {{2}} pessoa, {{3}} link da conversa.
VARIAVEIS_AVISO_PADRAO = {'1': '{voluntario}', '2': '{pessoa}', '3': '{link}'}

PERMISSOES_ATENDIMENTO = ('pode_conversar_pessoas', 'pode_ver_gabinete')


def atende_mensagens(usuario) -> bool:
    """Pode ser responsavel: conversa com as pessoas (perfil de mensagens, pastor ou admin).

    Quem so cadastra (sem esse perfil) nao pode ficar com a pessoa: ela ficaria presa
    com alguem que nao consegue responder.
    """
    if usuario is None or not usuario.is_active:
        return False
    return bool(
        usuario.is_staff
        or usuario.is_superuser
        or any(usuario.has_perm(f'acolhimento.{perm}') for perm in PERMISSOES_ATENDIMENTO)
    )


def _perfis_no_rodizio():
    from apps.core.models import PerfilUsuario

    com_permissao = get_user_model().objects.filter(
        Q(is_staff=True)
        | Q(is_superuser=True)
        | Q(user_permissions__codename__in=PERMISSOES_ATENDIMENTO, user_permissions__content_type__app_label='acolhimento')
    ).values('pk')
    return PerfilUsuario.objects.filter(
        recebe_novos_contatos=True,
        usuario__is_active=True,
        usuario__in=com_permissao,
    )


def equipe():
    """Usuarios no rodizio, na ordem em que vao receber."""
    perfis = _perfis_no_rodizio().select_related('usuario').order_by(
        F('ultima_atribuicao_em').asc(nulls_first=True), 'pk',
    )
    return [perfil.usuario for perfil in perfis]


def _nome(usuario) -> str:
    return usuario.first_name or usuario.get_full_name() or usuario.get_username()


def atribuir(pessoa: PrimeiroContato, motivo: str, *, avisar: bool = True):
    """Passa a pessoa sem responsavel para o proximo do rodizio. Devolve o usuario ou None."""
    if pessoa.responsavel_atual_id:
        return None
    with transaction.atomic():
        # Trava os perfis do rodizio: dois webhooks ao mesmo tempo nao pegam a mesma vez.
        perfil = (
            _perfis_no_rodizio()
            .select_for_update(of=('self',))
            .order_by(F('ultima_atribuicao_em').asc(nulls_first=True), 'pk')
            .first()
        )
        if perfil is None:
            return None
        atualizadas = PrimeiroContato.objects.filter(pk=pessoa.pk, responsavel_atual__isnull=True).update(
            responsavel_atual=perfil.usuario_id, atualizado_em=timezone.now(),
        )
        if not atualizadas:
            return None
        perfil.ultima_atribuicao_em = timezone.now()
        perfil.save(update_fields=['ultima_atribuicao_em', 'atualizado_em'])
        usuario = perfil.usuario
        passo = InteracaoAcolhimento.objects.create(
            pessoa=pessoa,
            tipo=InteracaoAcolhimento.TipoInteracao.OBSERVACAO,
            descricao=f'Responsável definido pelo rodízio: {usuario.get_full_name() or usuario.get_username()} ({motivo}).',
        )
        if avisar:
            transaction.on_commit(
                lambda: threading.Thread(
                    target=_avisar_em_thread, args=(pessoa.pk, usuario.pk, passo.pk), daemon=True,
                ).start()
            )
    pessoa.responsavel_atual = usuario
    logger.info('Rodizio: pessoa %s atribuida ao usuario %s (%s).', pessoa.pk, usuario.pk, motivo)
    return usuario


def ao_receber_mensagem(pessoa: PrimeiroContato):
    """Gancho do webhook: a pessoa escreveu. Nunca levanta excecao (o webhook segue)."""
    try:
        return atribuir(pessoa, 'mandou mensagem no WhatsApp')
    except Exception:  # noqa: BLE001
        logger.exception('Rodizio: falha ao atribuir a pessoa %s.', pessoa.pk)
        return None


def ao_cadastrar_pela_equipe(pessoa: PrimeiroContato, quem_cadastrou):
    """Cadastro feito por quem nao atende mensagens: a pessoa vai para o rodizio."""
    nome = quem_cadastrou.get_full_name() or quem_cadastrou.get_username()
    try:
        return atribuir(pessoa, f'cadastrada por {nome}, que não atende mensagens')
    except Exception:  # noqa: BLE001
        logger.exception('Rodizio: falha ao atribuir a pessoa %s.', pessoa.pk)
        return None


def ao_transferir(pessoa: PrimeiroContato, motivo: str):
    """O robo/IA passou a pessoa para a equipe: entra no rodizio se nao tem responsavel."""
    try:
        return atribuir(pessoa, f'o atendimento automático transferiu: {motivo}')
    except Exception:  # noqa: BLE001
        logger.exception('Rodizio: falha ao atribuir a pessoa %s.', pessoa.pk)
        return None


def ao_auto_cadastro(pessoa: PrimeiroContato):
    try:
        return atribuir(pessoa, 'se cadastrou pelo QR code')
    except Exception:  # noqa: BLE001
        logger.exception('Rodizio: falha ao atribuir a pessoa %s.', pessoa.pk)
        return None


# ---------------------------------------------------------------------------
# Aviso ao voluntario
# ---------------------------------------------------------------------------
def _marcadores(pessoa, usuario) -> dict[str, str]:
    link = settings.PUBLIC_BASE_URL + reverse('pessoas-mensagens', args=[pessoa.pk])
    return {
        'voluntario': _nome(usuario),
        'pessoa': (pessoa.nome or '').strip() or 'Uma pessoa',
        'link': link,
    }


def _aplicar(texto: str, marcadores: dict[str, str]) -> str:
    for chave, valor in marcadores.items():
        texto = texto.replace('{' + chave + '}', valor)
    return texto


def texto_aviso() -> str:
    return template_config.texto_evolution_para(Tipo.RESPONSAVEL_ATRIBUIDO) or TEXTO_AVISO_PADRAO


def parametros_aviso(marcadores: dict[str, str]) -> dict[str, str]:
    variaveis = template_config.variaveis_dict_para(Tipo.RESPONSAVEL_ATRIBUIDO) or VARIAVEIS_AVISO_PADRAO
    return {str(chave): _aplicar(str(valor), marcadores) for chave, valor in variaveis.items()}


def avisar(pessoa_id: int, usuario_id: int) -> str:
    """Manda o aviso agora. Devolve o resultado em texto (nunca levanta excecao)."""
    from apps.acolhimento.fila_processor import normalize_phone_br
    from apps.core.models import telefone_do_usuario

    pessoa = PrimeiroContato.objects.filter(pk=pessoa_id).first()
    usuario = get_user_model().objects.filter(pk=usuario_id).first()
    if pessoa is None or usuario is None:
        return ''
    telefone = telefone_do_usuario(usuario)
    if not telefone:
        return 'Aviso não enviado: responsável sem telefone no perfil.'
    marcadores = _marcadores(pessoa, usuario)
    try:
        destino = normalize_phone_br(telefone)
        if template_config.usando_meta():
            nome_template = template_config.meta_nome_para(Tipo.RESPONSAVEL_ATRIBUIDO)
            if nome_template:
                meta_service.send_template(
                    to_phone=destino,
                    nome=nome_template,
                    idioma=template_config.meta_idioma_para(Tipo.RESPONSAVEL_ATRIBUIDO),
                    parametros=parametros_aviso(marcadores),
                )
            else:
                meta_service.send_text(to_phone=destino, text=_aplicar(texto_aviso(), marcadores))
        elif template_config.usando_evolution():
            evolution_service.send_whatsapp_text(to_phone=destino, text=_aplicar(texto_aviso(), marcadores))
        else:
            raise RuntimeError('aviso só pela API da Meta ou pelo Evolution')
        return 'Aviso enviado por WhatsApp.'
    except Exception as exc:  # noqa: BLE001
        logger.warning('Rodizio: aviso da pessoa %s falhou.', pessoa_id, exc_info=True)
        return f'Aviso não enviado: {exc}'[:200]


def _avisar_em_thread(pessoa_id: int, usuario_id: int, passo_id: int) -> None:
    try:
        resultado = avisar(pessoa_id, usuario_id)
        passo = InteracaoAcolhimento.objects.filter(pk=passo_id).first()
        if passo and resultado:
            passo.descricao = f'{passo.descricao} {resultado}'
            passo.save(update_fields=['descricao'])
    except Exception:  # noqa: BLE001
        logger.exception('Rodizio: falha no aviso da pessoa %s.', pessoa_id)
    finally:
        close_old_connections()


# ---------------------------------------------------------------------------
# Atrasados: quem escreveu nos ultimos dias e ainda esta sem responsavel
# ---------------------------------------------------------------------------
DIAS_ATRASADOS = 7


def sem_responsavel_que_escreveram(dias: int = DIAS_ATRASADOS):
    desde = timezone.now() - timedelta(days=dias)
    escreveram = MensagemContato.objects.filter(
        direcao=MensagemContato.DirecaoChoices.ENTRADA, enfileirada_em__gte=desde,
    ).values('pessoa_id')
    return (
        PrimeiroContato.objects.filter(responsavel_atual__isnull=True, pk__in=escreveram)
        .exclude(status=PrimeiroContato.StatusAcolhimento.MEMBRO)
        .order_by('pk')
    )


def distribuir_atrasados(dias: int = DIAS_ATRASADOS) -> dict[str, int]:
    """Distribui de uma vez, sem mandar um aviso por pessoa. Devolve {nome: quantas}."""
    resultado: dict[str, int] = {}
    for pessoa in sem_responsavel_que_escreveram(dias):
        usuario = atribuir(pessoa, 'distribuição dos que estavam sem responsável', avisar=False)
        if usuario is None:
            break
        nome = usuario.get_full_name() or usuario.get_username()
        resultado[nome] = resultado.get(nome, 0) + 1
    return resultado
