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

### Perfil local (HTTP, porta 8080)

Use o arquivo `.env.local` (ja incluido no projeto) e rode:

```bash
docker-compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

Acesse:

- `http://localhost:8080/login/`

### Perfil produção (HTTPS via proxy)

Crie seu `.env.prod` a partir de `.env.prod.example` e rode:

```bash
cp .env.prod.example .env.prod
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### Ver status e logs

```bash
docker-compose -f docker-compose.yml -f docker-compose.local.yml ps
docker-compose -f docker-compose.yml -f docker-compose.local.yml logs -f web
```

### Parar aplicação

```bash
docker-compose -f docker-compose.yml -f docker-compose.local.yml down
```

## Variáveis de ambiente

Principais variáveis usadas pelo projeto:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `DJANGO_SECURE_SSL_REDIRECT`
- `SQLITE_PATH`

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
```

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