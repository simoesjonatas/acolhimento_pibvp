"""Custo do WhatsApp na API oficial da Meta.

Duas fontes, mostradas lado a lado no relatorio (somente superusuario):

1. Custo oficial: a propria Meta informa o custo aproximado por periodo
   (`pricing_analytics` da WABA), na moeda da conta de pagamento.
2. Detalhamento interno: cada confirmacao de status do webhook traz o objeto
   `pricing` (categoria + tipo regular/gratuito). Gravamos isso na mensagem e
   estimamos o custo com a tabela de precos editavel (`TabelaPrecoWhatsapp`),
   o que permite quebrar por origem (boas-vindas, continuacao, disparo, bot...).

Regras de cobranca (por mensagem, desde jul/2025; ajustes de out/2026):
  - `pricing.type == 'regular'` => cobrada; `free_*` => gratuita.
  - Servico (resposta livre) e gratuito ate a franquia mensal do numero; acima
    dela, a Meta passa a mandar `type=regular`.
  - Mensagem com falha nao e cobrada (o webhook de falha nao traz `pricing`).
"""
from __future__ import annotations

import csv
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from apps.acolhimento.models import MensagemContato, TabelaPrecoWhatsapp, TemplateWhatsapp

# Rotulos das categorias de cobranca da Meta.
CATEGORIAS = OrderedDict([
    ('marketing', 'Marketing'),
    ('utility', 'Utilidade'),
    ('authentication', 'Autenticacao'),
    ('service', 'Servico (conversa)'),
])

# Categorias "derivadas" que a Meta usa e o preco base que aplicamos a elas.
_CATEGORIA_BASE = {
    'marketing': 'marketing',
    'marketing_lite': 'marketing',
    'group_marketing': 'marketing',
    'utility': 'utility',
    'group_utility': 'utility',
    'authentication': 'authentication',
    'authentication-international': 'authentication',
    'authentication_international': 'authentication',
    'service': 'service',
    'group_service': 'service',
}

TIPOS = {
    'regular': 'Cobrada',
    'free_customer_service': 'Gratuita (atendimento)',
    'free_group_customer_service': 'Gratuita (atendimento em grupo)',
    'free_entry_point': 'Gratuita (anuncio)',
}

# Origem da mensagem no sistema (para saber o que gera custo).
ORIGENS = OrderedDict([
    ('primeiro_contato', 'Boas-vindas (primeiro contato)'),
    ('continuar', 'Continuar conversa'),
    ('marketing', 'Disparo em massa (template)'),
    ('disparo_livre', 'Disparo em massa (texto/questionario)'),
    ('bot', 'Atendimento automatico'),
    ('equipe', 'Conversa da equipe'),
])

MAX_DIAS_PERIODO = 366


# ---------------------------------------------------------------------------
# Gravacao (chamado pelo webhook)
# ---------------------------------------------------------------------------
def extrair_cobranca(status: dict) -> dict | None:
    """Normaliza o objeto `pricing` de um status do webhook. None se nao houver."""
    pricing = status.get('pricing') if isinstance(status, dict) else None
    if not isinstance(pricing, dict):
        return None
    tipo = str(pricing.get('type') or '').strip().lower()
    if not tipo and pricing.get('billable') is not None:
        # Webhooks antigos (pre-PMP) so tinham `billable`.
        tipo = 'regular' if pricing.get('billable') else 'free_customer_service'
    return {
        'categoria': str(pricing.get('category') or '').strip().lower()[:40],
        'tipo': tipo[:40],
        'modelo': str(pricing.get('pricing_model') or '').strip().upper()[:10],
    }


def aplicar_cobranca(mensagem: MensagemContato, status: dict) -> list[str]:
    """Grava a cobranca do status na mensagem (sem salvar). Devolve os campos alterados."""
    cobranca = extrair_cobranca(status)
    if not cobranca:
        return []
    mensagem.cobranca_categoria = cobranca['categoria']
    mensagem.cobranca_tipo = cobranca['tipo']
    mensagem.cobranca_modelo = cobranca['modelo']
    mensagem.cobranca_registrada_em = timezone.now()
    return ['cobranca_categoria', 'cobranca_tipo', 'cobranca_modelo', 'cobranca_registrada_em']


# ---------------------------------------------------------------------------
# Classificacao
# ---------------------------------------------------------------------------
def categoria_base(categoria: str) -> str:
    return _CATEGORIA_BASE.get((categoria or '').strip().lower(), '')


def rotulo_categoria(categoria: str) -> str:
    base = categoria_base(categoria)
    if base:
        return CATEGORIAS[base]
    return categoria or 'Sem categoria'


def origem_da_mensagem(metadata: dict | None) -> str:
    metadata = metadata or {}
    tipo_template = metadata.get('tipo_template')
    if tipo_template == TemplateWhatsapp.Tipo.PRIMEIRO_CONTATO:
        return 'primeiro_contato'
    if tipo_template == TemplateWhatsapp.Tipo.CONTINUAR:
        return 'continuar'
    if tipo_template == 'marketing_massa':
        return 'marketing'
    origem = metadata.get('origem')
    if origem == 'bot_atendimento':
        return 'bot'
    if origem == 'disparo_massa':
        return 'disparo_livre'
    return 'equipe'


def preco_unitario(tabela: TabelaPrecoWhatsapp, categoria: str) -> Decimal:
    base = categoria_base(categoria)
    if not base:
        return Decimal('0')
    return Decimal(getattr(tabela, f'preco_{base}') or 0)


# ---------------------------------------------------------------------------
# Periodo
# ---------------------------------------------------------------------------
def periodo_padrao(hoje: date | None = None) -> tuple[date, date]:
    hoje = hoje or timezone.localdate()
    return hoje.replace(day=1), hoje


def periodo_mes_anterior(hoje: date | None = None) -> tuple[date, date]:
    hoje = hoje or timezone.localdate()
    fim = hoje.replace(day=1) - timedelta(days=1)
    return fim.replace(day=1), fim


def _limites(inicio: date, fim: date) -> tuple[datetime, datetime]:
    """[inicio 00:00, fim+1 00:00) no fuso local, como datetimes aware."""
    tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(inicio, time.min), tz),
        timezone.make_aware(datetime.combine(fim + timedelta(days=1), time.min), tz),
    )


def mensagens_do_periodo(inicio: date, fim: date):
    """Saidas de WhatsApp enviadas pela Meta no periodo (com ou sem cobranca confirmada)."""
    de, ate = _limites(inicio, fim)
    return (
        MensagemContato.objects.select_related('pessoa')
        .filter(
            canal=MensagemContato.CanalChoices.WHATSAPP,
            direcao=MensagemContato.DirecaoChoices.SAIDA,
            enviada_em__gte=de,
            enviada_em__lt=ate,
        )
        .filter(Q(metadata_envio__has_key='meta') | ~Q(cobranca_categoria=''))
        .order_by('enviada_em', 'id')
    )


# ---------------------------------------------------------------------------
# Resumo
# ---------------------------------------------------------------------------
@dataclass
class Linha:
    chave: str
    rotulo: str
    total: int = 0
    cobradas: int = 0
    gratuitas: int = 0
    sem_confirmacao: int = 0
    falhas: int = 0
    custo_estimado: Decimal = field(default_factory=lambda: Decimal('0'))
    custo_oficial: Decimal | None = None

    def somar(self, item: dict) -> None:
        self.total += 1
        if item['falha']:
            self.falhas += 1
        elif item['cobravel'] is None:
            self.sem_confirmacao += 1
        elif item['cobravel']:
            self.cobradas += 1
            self.custo_estimado += item['custo']
        else:
            self.gratuitas += 1


def _item(mensagem: MensagemContato, tabela: TabelaPrecoWhatsapp) -> dict:
    falha = mensagem.status_fila == MensagemContato.StatusFilaChoices.FALHA
    cobravel = None if falha else mensagem.cobravel
    custo = preco_unitario(tabela, mensagem.cobranca_categoria) if cobravel else Decimal('0')
    return {
        'mensagem': mensagem,
        'falha': falha,
        'cobravel': cobravel,
        'categoria': categoria_base(mensagem.cobranca_categoria) or (mensagem.cobranca_categoria or ''),
        'origem': origem_da_mensagem(mensagem.metadata_envio),
        'custo': custo,
    }


def resumo(inicio: date, fim: date, *, tabela: TabelaPrecoWhatsapp | None = None, oficial: dict | None = None) -> dict:
    """Agrega o periodo por categoria, por origem e por dia."""
    tabela = tabela or TabelaPrecoWhatsapp.carregar()
    total = Linha('total', 'Total')
    por_categoria: dict[str, Linha] = OrderedDict(
        (chave, Linha(chave, rotulo)) for chave, rotulo in CATEGORIAS.items()
    )
    por_origem: dict[str, Linha] = OrderedDict(
        (chave, Linha(chave, rotulo)) for chave, rotulo in ORIGENS.items()
    )
    por_dia: dict[date, Linha] = {}

    for mensagem in mensagens_do_periodo(inicio, fim):
        item = _item(mensagem, tabela)
        total.somar(item)
        categoria = item['categoria']
        if categoria:
            if categoria not in por_categoria:
                por_categoria[categoria] = Linha(categoria, rotulo_categoria(categoria))
            por_categoria[categoria].somar(item)
        por_origem[item['origem']].somar(item)
        dia = timezone.localtime(mensagem.enviada_em).date()
        if dia not in por_dia:
            por_dia[dia] = Linha(dia.isoformat(), dia.strftime('%d/%m/%Y'))
        por_dia[dia].somar(item)

    if oficial and oficial.get('por_categoria'):
        for categoria, valores in oficial['por_categoria'].items():
            if categoria not in por_categoria:
                por_categoria[categoria] = Linha(categoria, rotulo_categoria(categoria))
            por_categoria[categoria].custo_oficial = valores['custo']

    return {
        'inicio': inicio,
        'fim': fim,
        'tabela': tabela,
        'total': total,
        'por_categoria': [linha for linha in por_categoria.values() if linha.total or linha.custo_oficial],
        'por_origem': [linha for linha in por_origem.values() if linha.total],
        'por_dia': [por_dia[dia] for dia in sorted(por_dia)],
        'oficial': oficial,
    }


# ---------------------------------------------------------------------------
# Custo oficial (Meta pricing_analytics)
# ---------------------------------------------------------------------------
def custo_oficial(inicio: date, fim: date) -> dict | None:
    """Custo aproximado informado pela Meta no periodo, por categoria. None se indisponivel.

    Cacheado por 10 min: a Meta consolida os numeros com algum atraso e a tela nao
    precisa consultar a cada recarga.
    """
    from apps.acolhimento import meta_service

    chave = f'wa_custo_oficial:{inicio.isoformat()}:{fim.isoformat()}'
    em_cache = cache.get(chave, '__miss__')
    if em_cache != '__miss__':
        return em_cache

    de, ate = _limites(inicio, fim)
    pontos = meta_service.get_pricing_analytics(int(de.timestamp()), int(ate.timestamp()))
    if pontos is None:
        cache.set(chave, None, 120)
        return None

    total_custo = Decimal('0')
    total_volume = 0
    por_categoria: dict[str, dict] = defaultdict(lambda: {'custo': Decimal('0'), 'volume': 0})
    for ponto in pontos:
        custo = Decimal(str(ponto.get('cost') or 0))
        volume = int(ponto.get('volume') or 0)
        categoria = categoria_base(str(ponto.get('pricing_category') or '')) or str(
            ponto.get('pricing_category') or ''
        ).lower()
        total_custo += custo
        total_volume += volume
        por_categoria[categoria]['custo'] += custo
        por_categoria[categoria]['volume'] += volume

    resultado = {
        'custo': total_custo,
        'volume': total_volume,
        'por_categoria': dict(por_categoria),
    }
    cache.set(chave, resultado, 600)
    return resultado


# ---------------------------------------------------------------------------
# Exportacao
# ---------------------------------------------------------------------------
def escrever_csv(destino, inicio: date, fim: date, *, tabela: TabelaPrecoWhatsapp | None = None) -> None:
    """Uma linha por mensagem do periodo (para conferencia/contabilidade)."""
    tabela = tabela or TabelaPrecoWhatsapp.carregar()
    writer = csv.writer(destino)
    writer.writerow([
        'Enviada em',
        'Pessoa',
        'Telefone',
        'Origem',
        'Categoria Meta',
        'Situacao',
        f'Custo estimado ({tabela.moeda})',
        'Id da mensagem (wamid)',
    ])
    for mensagem in mensagens_do_periodo(inicio, fim):
        item = _item(mensagem, tabela)
        if item['falha']:
            situacao = 'Falha (nao cobrada)'
        elif item['cobravel'] is None:
            situacao = 'Sem confirmacao da Meta'
        else:
            situacao = TIPOS.get(mensagem.cobranca_tipo, mensagem.cobranca_tipo or '')
        writer.writerow([
            timezone.localtime(mensagem.enviada_em).strftime('%d/%m/%Y %H:%M'),
            mensagem.pessoa.nome,
            mensagem.pessoa.telefone_whatsapp,
            ORIGENS[item['origem']],
            rotulo_categoria(mensagem.cobranca_categoria) if mensagem.cobranca_categoria else '',
            situacao,
            f"{item['custo']:.4f}".replace('.', ','),
            mensagem.referencia_externa,
        ])
