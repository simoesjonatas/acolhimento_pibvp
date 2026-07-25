# Acolhimento PIBVP

Sistema de acolhimento da igreja PIBVP, desenvolvido em Django.

## Requisitos

- Python 3.10+
- pip
- Docker + docker-compose (opcional)

## Estrutura principal

- `apps/core/`: autenticação e dashboard
- `apps/acolhimento/`: cadastro de pessoas e timeline de acompanhamento
- `config/`: settings e urls do projeto

Guia detalhado de arquitetura e organização:

- [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)

## 1) Rodar local (sem Docker)

### Criar e ativar ambiente virtual

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Instalar dependências

```bash
pip install -r requirements.txt
```

### Aplicar migrações

```bash
python manage.py migrate
```

### Criar superusuário

```bash
python manage.py createsuperuser
```

### Subir servidor

```bash
python manage.py runserver
```

Acesse:

- `http://localhost:8000/login/`

## 2) Rodar com Docker

O compose principal agora esta em `docker-compose.yml` e sobe a aplicacao com um banco Postgres dedicado.

Crie seu `.env` a partir do exemplo, ajuste as chaves/senhas e rode:

```bash
cp .env.example .env
docker compose up -d --build
```

Acesse:

- `http://localhost:8000/login/`

### Ver status e logs

```bash
docker compose ps
docker compose logs -f web
```

### Parar aplicação

```bash
docker compose down
```

O banco fica persistido no volume Docker `pibvp-acolhimento_postgres_data`.

### Compose legado

Os overlays antigos foram movidos para `docker/legacy/` apenas como referencia. O fluxo recomendado e usar o `docker-compose.yml` unico da raiz.

## Variáveis de ambiente

Principais variáveis usadas pelo projeto:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DJANGO_SECURE_SSL_REDIRECT`
- `SQLITE_PATH`
- `APP_PORT`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `TWILIO_ENABLED`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_FROM`
- `TWILIO_STATUS_CALLBACK_URL` (opcional em local; use uma URL pública real para webhooks)
- `TWILIO_TEMPLATE_OPT_IN_SID` (Content SID do template aprovado)
- `TWILIO_TEMPLATE_OPT_IN_VARIABLES` (JSON string com variáveis do template)

Exemplo para produção (domínio oficial):

- Host: `acolhimento.simoesti.com.br`
- URL: `https://acolhimento.simoesti.com.br/`

## Deploy e HTTPS

O projeto está preparado para rodar atrás de proxy reverso com HTTPS (Nginx, Traefik, NPM), usando:

- `SECURE_PROXY_SSL_HEADER`
- `USE_X_FORWARDED_HOST`
- cookies seguros e HSTS quando `DEBUG=False`

## Comandos úteis

```bash
# checar configuração Django
python manage.py check

# gerar migrações
python manage.py makemigrations

# aplicar migrações
python manage.py migrate

# testar processamento da fila sem enviar mensagens reais
python manage.py processar_fila_mensagens --dry-run --limit 5

# processar fila com envio real
python manage.py processar_fila_mensagens --limit 5
```

## Processamento da fila pela interface web

Usuarios administrativos podem acessar:

- `/acolhimento/mensagens/processamento/`

Nessa tela e possivel:

- iniciar um processamento manual (com limite e dry-run)
- solicitar parada de uma execucao em andamento
- visualizar historico e log de cada execucao

## Problemas comuns

### 1. Não abre em `localhost:8000`

- Verifique se o container está `Up`: `docker-compose ps`
- Verifique logs: `docker-compose logs --tail=100 web`
- Garanta no `.env` local:
	- `DJANGO_DEBUG=True`
	- `DJANGO_SECURE_SSL_REDIRECT=False`

### 2. Erro de recriação no docker-compose legado (`ContainerConfig`)

```bash
docker-compose down --remove-orphans
docker rm -f pibvp-acolhimento-web || true
docker-compose up -d
```

## Fluxo de contribuição sugerido

```bash
git checkout -b feature/nome-da-feature
git add .
git commit -m "feat: descricao da feature"
git push origin feature/nome-da-feature
```
