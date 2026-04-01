# Estrutura do Projeto - Guia para Desenvolvedores

Este documento explica como o sistema esta organizado e como evoluir features sem se perder no codigo.

## Visao geral

O projeto foi dividido em apps Django para separar responsabilidades:

- `apps/core`: autenticacao e dashboard
- `apps/acolhimento`: regras de negocio do acolhimento (pessoas, timeline e filtros)
- `config`: configuracao global (settings, urls, wsgi/asgi)
- `templates`: telas HTML
- `static`: CSS e assets visuais

Objetivo da separacao:

- manter cada dominio com seu proprio codigo
- facilitar manutencao por multiplos desenvolvedores
- reduzir impacto de mudancas em outras areas

## Mapa de pastas

```text
acolhimento_pibvp/
  apps/
    core/
      views.py
      urls.py
    acolhimento/
      models.py
      forms.py
      views.py
      urls.py
      admin.py
      migrations/
  config/
    settings.py
    urls.py
    wsgi.py
  templates/
    base.html
    login.html
    dashboard.html
    pessoas.html
    pessoa_form.html
    pessoa_detail.html
    pessoa_confirm_delete.html
  static/
    css/
      base.css
      pages.css
  manage.py
  Dockerfile
  docker-compose.yml
```

## Fluxo da aplicacao (request -> resposta)

1. URL chega em `config/urls.py`
2. O Django delega para `apps/core/urls.py` ou `apps/acolhimento/urls.py`
3. A view processa dados e regras em `views.py`
4. Se necessario, usa modelos em `models.py`
5. Renderiza HTML em `templates/*.html`
6. Estilo visual vem de `static/css/*.css`

## Dominio de negocio (acolhimento)

### Modelos principais

- `PrimeiroContato`:
  - cadastro inicial simplificado
  - status de acolhimento
  - base para evolucao futura do cadastro

- `InteracaoAcolhimento`:
  - timeline de passos da pessoa
  - historico de tentativas, respostas, visitas e observacoes

### Features atuais

- login/logout
- dashboard com indicadores
- CRUD completo de pessoa
- timeline por pessoa
- busca, filtro e ordenacao na listagem
- paginacao
- exportacao CSV

## Onde mexer para cada tipo de tarefa

### 1. Nova tela

- criar template em `templates/`
- criar view em `apps/.../views.py`
- mapear rota em `apps/.../urls.py`

### 2. Novo campo de dados

- alterar modelo em `apps/acolhimento/models.py`
- atualizar formulario em `apps/acolhimento/forms.py`
- gerar migration:

```bash
python manage.py makemigrations
python manage.py migrate
```

### 3. Novo filtro na listagem de pessoas

- ajustar `PrimeiroContatoQuerysetMixin` em `apps/acolhimento/views.py`
- atualizar formulario de filtro em `templates/pessoas.html`

### 4. Novo card na dashboard

- calcular dado em `apps/core/views.py`
- exibir no `templates/dashboard.html`

### 5. Ajuste visual

- estilo base global em `static/css/base.css`
- estilos de paginas em `static/css/pages.css`

## Convencoes adotadas

- nomes de classes e metodos claros e orientados ao dominio
- logica de listagem centralizada em mixin para reutilizacao
- feedback ao usuario com mensagens (toasts)
- layout responsivo priorizando uso em mobile

## Como criar uma feature sem baguncar

1. Entender em qual app a mudanca pertence (`core` ou `acolhimento`)
2. Alterar primeiro o backend (model/view/url)
3. Depois ajustar template
4. Ajustar CSS so no necessario
5. Rodar validacao:

```bash
python manage.py check
```

6. Testar fluxo no navegador

## Pontos de atencao para o time

- nao colocar regra de negocio no template
- evitar duplicar logica de filtro/ordenacao
- manter mensagens de sucesso/erro consistentes
- revisar impacto em mobile sempre que alterar layout
- em producao, validar variaveis do `.env`

## Proximas evolucoes previstas

- integracao com WhatsApp
- automacao com chatbot
- cadastro progressivo (campos por etapa)
- historico mais completo de atendimento

Esse guia deve ser atualizado sempre que uma feature estrutural for adicionada.
