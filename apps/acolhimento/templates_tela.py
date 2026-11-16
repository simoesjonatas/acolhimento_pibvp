"""Tela "Mensagens do WhatsApp": cartoes por template, abas e status na Meta.

O formulario (TemplatesWhatsappForm) continua com os campos planos de sempre
(`opt_in_meta_nome`, `gab_pastor_texto`...). Aqui eles sao agrupados em cartoes e
abas so para exibir; o status na Meta e a previa do texto vem por uma consulta
separada (status_cartoes), para a Meta lenta ou fora do ar nao travar a tela.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.conf import settings
from django.core.cache import cache

from apps.acolhimento import meta_service, template_config
from apps.acolhimento.models import TemplateWhatsapp

Tipo = TemplateWhatsapp.Tipo

ABAS = [
    ('conversa', 'Boas-vindas e conversa', 'Conversa'),
    ('gabinete', 'Gabinete', 'Gabinete'),
    ('rodizio', 'Rodízio', 'Rodízio'),
]
ABAS_VALIDAS = [slug for slug, _, _ in ABAS]

# Valores de exemplo para a previa (nunca dados reais).
EXEMPLOS = {
    'nome': 'Maria',
    'pessoa': 'Maria Souza',
    'data': '20/11/2026',
    'hora': '19:30',
    'pastor': 'Pr. João',
    'voluntario': 'Ana',
}

CARTOES = [
    {
        'slug': 'opt_in', 'tipo': Tipo.PRIMEIRO_CONTATO, 'aba': 'conversa',
        'titulo': 'Boas-vindas (primeiro contato)',
        'descricao': 'Abre a conversa com quem está em Primeiro contato: botão "Disparar boas-vindas" e envio automático do painel.',
        'texto_rotulo': 'Mensagem de boas-vindas no WhatsApp', 'texto_so_evolution': True,
        'marcadores': ['nome'], 'nome_vai_na_1': True, 'tem_sid': True,
        'exemplo_nome': 'boas_vindas_pibvp',
    },
    {
        'slug': 'continuar', 'tipo': Tipo.CONTINUAR, 'aba': 'conversa',
        'titulo': 'Continuar conversa',
        'descricao': 'Reabre a janela de 24h quando a pessoa parou de responder (botão na conversa).',
        'texto_rotulo': 'Mensagem de continuacao no WhatsApp', 'texto_so_evolution': True,
        'marcadores': ['nome'], 'nome_vai_na_1': True, 'tem_sid': True,
        'exemplo_nome': 'continuar_conversa_pibvp',
    },
    {
        'slug': 'gab_pessoa', 'tipo': Tipo.GABINETE_PESSOA, 'aba': 'gabinete',
        'titulo': 'Gabinete agendado — para a pessoa',
        'descricao': 'Confirmação ao agendar um gabinete. Com a janela de 24h aberta sai o texto livre; fechada, só pelo template.',
        'texto_rotulo': 'Mensagem (texto livre)', 'texto_so_evolution': False,
        'marcadores': ['nome', 'data', 'hora', 'pastor'], 'nome_vai_na_1': False, 'tem_sid': False,
        'exemplo_nome': 'gabinete_agendado_pessoa',
    },
    {
        'slug': 'gab_pastor', 'tipo': Tipo.GABINETE_PASTOR, 'aba': 'gabinete',
        'titulo': 'Gabinete agendado — aviso ao pastor',
        'descricao': 'Vai para o telefone do perfil do pastor. Sem template, o texto só chega se o pastor escreveu para o número nas últimas 24h.',
        'texto_rotulo': 'Mensagem (texto livre)', 'texto_so_evolution': False,
        'marcadores': ['pessoa', 'data', 'hora', 'pastor'], 'nome_vai_na_1': False, 'tem_sid': False,
        'exemplo_nome': 'gabinete_agendado_pastor',
    },
    {
        'slug': 'resp', 'tipo': Tipo.RESPONSAVEL_ATRIBUIDO, 'aba': 'rodizio',
        'titulo': 'Rodízio — aviso ao responsável',
        'descricao': 'Vai para o telefone do perfil de quem recebeu a pessoa pelo rodízio. Sem template, só chega se o voluntário escreveu para o número nas últimas 24h.',
        'texto_rotulo': 'Mensagem (texto livre)', 'texto_so_evolution': False,
        'marcadores': ['voluntario', 'pessoa', 'link'], 'nome_vai_na_1': False, 'tem_sid': False,
        'exemplo_nome': 'nova_pessoa_atribuida',
    },
]

# Campo de texto de cada cartao no formulario.
_CAMPO_TEXTO = {
    'opt_in': 'opt_in_texto_evolution',
    'continuar': 'continuar_texto_evolution',
    'gab_pessoa': 'gab_pessoa_texto',
    'gab_pastor': 'gab_pastor_texto',
    'resp': 'resp_texto',
}


def _campos(slug):
    return {
        'nome': f'{slug}_meta_nome',
        'idioma': f'{slug}_meta_idioma',
        'variables': f'{slug}_variables',
        'sid': f'{slug}_sid',
        'texto': _CAMPO_TEXTO[slug],
    }


def montar_abas(form, *, usa_meta, usa_twilio, usa_evolution, configs, env=None):
    """Lista de abas, cada uma com seus cartoes ja ligados aos campos do formulario."""
    por_aba = {slug: {'slug': slug, 'rotulo': rotulo, 'rotulo_curto': curto, 'cartoes': [], 'erros': False}
               for slug, rotulo, curto in ABAS}
    for definicao in CARTOES:
        nomes = _campos(definicao['slug'])
        campos = {chave: form[nome] for chave, nome in nomes.items() if nome in form.fields}
        erro = {chave: bool(campo.errors) for chave, campo in campos.items()}
        cartao = {
            **definicao,
            'campos': campos,
            'mostra_meta': usa_meta or erro.get('nome') or erro.get('idioma'),
            'mostra_sid': definicao['tem_sid'] and (usa_twilio or erro.get('sid')),
            'mostra_variaveis': (
                usa_meta or (definicao['tem_sid'] and usa_twilio) or erro.get('variables')
            ),
            'variaveis_com_erro': erro.get('variables'),
            'mostra_texto': (not definicao['texto_so_evolution']) or usa_evolution or erro.get('texto'),
            'config': configs.get(definicao['tipo']),
            'env': (env or {}).get(definicao['slug'], {}),
            'tem_erro': any(erro.values()),
        }
        aba = por_aba[definicao['aba']]
        aba['cartoes'].append(cartao)
        aba['erros'] = aba['erros'] or cartao['tem_erro']
    return list(por_aba.values())


# ---------------------------------------------------------------------------
# Status na Meta e previa (consulta separada, em JSON)
# ---------------------------------------------------------------------------
_CACHE_LISTA = 'meta_templates_lista_tela'

_STATUS = {
    'APPROVED': ('Aprovado na Meta', 'ok'),
    'PENDING': ('Em análise na Meta', 'espera'),
    'IN_APPEAL': ('Em recurso na Meta', 'espera'),
    'REJECTED': ('Rejeitado pela Meta', 'erro'),
    'PAUSED': ('Pausado pela Meta', 'erro'),
    'DISABLED': ('Desativado pela Meta', 'erro'),
}


def _lista_templates():
    lista = cache.get(_CACHE_LISTA)
    if lista is None:
        lista = meta_service.list_templates()
        # Lista vazia (Meta fora do ar) fica pouco tempo no cache.
        cache.set(_CACHE_LISTA, lista, 120 if lista else 20)
    return lista


def _marcadores_exemplo(pessoa_pk: int = 123) -> dict[str, str]:
    return {
        **EXEMPLOS,
        'link': f'{settings.PUBLIC_BASE_URL}/acolhimento/pessoas/{pessoa_pk}/mensagens/',
    }


def _aplicar(texto: str, marcadores: dict[str, str]) -> str:
    for chave, valor in marcadores.items():
        texto = texto.replace('{' + chave + '}', valor)
    return texto


def _variaveis_padrao(tipo) -> dict:
    from apps.acolhimento import gabinete_avisos, rodizio

    return {
        Tipo.GABINETE_PESSOA: gabinete_avisos.VARIAVEIS_PESSOA_PADRAO,
        Tipo.GABINETE_PASTOR: gabinete_avisos.VARIAVEIS_PASTOR_PADRAO,
        Tipo.RESPONSAVEL_ATRIBUIDO: rodizio.VARIAVEIS_AVISO_PADRAO,
    }.get(tipo, {})


def parametros_exemplo(definicao) -> dict[str, str]:
    tipo = definicao['tipo']
    if definicao['nome_vai_na_1']:
        return template_config.parametros_meta_para(tipo, SimpleNamespace(nome=EXEMPLOS['nome']))
    marcadores = _marcadores_exemplo()
    variaveis = template_config.variaveis_dict_para(tipo) or _variaveis_padrao(tipo)
    return {str(chave): _aplicar(str(valor), marcadores) for chave, valor in variaveis.items()}


def status_cartoes() -> dict:
    """{slug: {rotulo, classe, previa, aviso}} + nomes aprovados, para a tela preencher."""
    if not template_config.usando_meta():
        return {'cartoes': {}, 'aprovados': []}
    lista = _lista_templates()
    por_nome = {(item.get('name'), item.get('language')): item for item in lista}
    aprovados = sorted({item.get('name') for item in lista if item.get('status') == 'APPROVED' and item.get('name')})

    resultado = {}
    for definicao in CARTOES:
        tipo = definicao['tipo']
        nome = template_config.meta_nome_para(tipo)
        idioma = template_config.meta_idioma_para(tipo)
        info = {'rotulo': '', 'classe': '', 'previa': '', 'aviso': ''}
        if not nome:
            info.update(rotulo='Sem template', classe='neutro')
        elif not lista:
            info.update(rotulo='Não deu para consultar a Meta', classe='espera')
        elif (nome, idioma) not in por_nome:
            outros = sorted({lang for (n, lang) in por_nome if n == nome})
            info.update(rotulo='Não encontrado na Meta', classe='erro')
            if outros:
                info['aviso'] = f'Existe com outro idioma: {", ".join(outros)}.'
            else:
                info['aviso'] = 'Confira se o nome está exatamente igual ao do painel da Meta.'
        else:
            status = (por_nome[(nome, idioma)].get('status') or '').upper()
            info['rotulo'], info['classe'] = _STATUS.get(status, (status.title() or 'Desconhecido', 'espera'))
            if status == 'APPROVED':
                info.update(_previa(definicao, nome, idioma))
        resultado[definicao['slug']] = info
    return {'cartoes': resultado, 'aprovados': aprovados}


def _previa(definicao, nome, idioma) -> dict[str, str]:
    template = template_config._template_meta_cacheado(nome, idioma)
    if not template:
        return {}
    parametros = parametros_exemplo(definicao)
    saida = {'previa': meta_service.renderizar_template(template, parametros)}
    requisitos = meta_service.requisitos_template(template)
    esperadas = requisitos.get('corpo_posicionais') or 0
    if esperadas != len(parametros):
        saida['aviso'] = (
            f'O template tem {esperadas} variável(is) e o sistema manda {len(parametros)}: '
            'confira as variáveis em "Avançado", senão a Meta recusa o envio.'
        )
    return saida

