# Plano: Pré-visualizar o Content Template SID (Twilio) no disparo em massa

> **Status:** planejado, ainda **não implementado**. Preparado em 2026-08-01 para execução no futuro próximo.
> **Decisão do produto:** preview **completo** (corpo + status de aprovação + categoria + variáveis + botões + prévia renderizada com as variáveis digitadas; "não encontrado" se o SID não existir).

## Contexto / problema
No disparo em massa (modo **Template / marketing**, em `templates/mensagens_disparo_massa.html`), o usuário cola um **Content Template SID** (`HX...`) sem nenhuma forma de conferir:
- se o SID **existe** na conta Twilio;
- **o que está escrito** no template;
- se foi **aprovado pela Meta** (e em qual categoria).

Um SID errado, não aprovado ou de categoria errada gera envios que falham (ex.: erro 63016) e **gastam dinheiro** à toa. Objetivo: ao digitar o SID, **pré-visualizar o template como está cadastrado — ou saber que não está**.

## Viabilidade (já pesquisado)
O projeto usa o SDK oficial `twilio==9.2.3` (ver `requirements.txt`), que expõe a **Content API**. Confirmado por introspecção do SDK instalado:

- **Buscar o template:** `client.content.v1.contents(sid).fetch()` → `ContentInstance` com os campos:
  - `sid`, `friendly_name`, `language`
  - `variables` — dict de variáveis de exemplo, ex.: `{"1": "exemplo"}`
  - `types` — dict com a estrutura; o corpo fica em `types["twilio/text"]["body"]` (ou `twilio/quick-reply`, `twilio/call-to-action`, `twilio/card` → cada um tem `body`/`title` e às vezes `actions` com os botões).
- **Status de aprovação (Meta/WhatsApp):** `client.content.v1.contents(sid).approval_fetch().fetch()` → `ApprovalFetchInstance` com `whatsapp` = dict contendo `status` (approved/pending/rejected/…), `category` (MARKETING/UTILITY/…), `rejection_reason`.
- **SID inexistente:** `fetch()` lança `TwilioRestException` com `status == 404`.

## Abordagem

### 1. `apps/acolhimento/twilio_service.py` — leitura read-only da Content API
Reusar `_load_twilio_sdk()` e o padrão de `_build_client()` (linhas ~13–41). Adicionar:

- `_build_readonly_client()` — igual a `_build_client()`, mas **sem exigir `TWILIO_ENABLED`** (preview é somente leitura, não envia nada; basta ter `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN`). Mesma mensagem de erro para credenciais ausentes.
- `_extrair_corpo_botoes(types: dict) -> (body, buttons, tipo_nome)` — pega o primeiro tipo de `types`; `body` de `.get("body")` com fallback `title`; `buttons` = títulos de `actions` (quick-reply/call-to-action/card).
- `fetch_content_template(content_sid) -> dict`:
  - valida SID não-vazio;
  - `contents(sid).fetch()`; em `TwilioRestException` com `status == 404` → `{"exists": False}`; outros erros → `TwilioWhatsAppError(str(e))`;
  - busca a aprovação em `try/except` separado (pode não existir → `approval` vazio);
  - retorna: `{exists, sid, friendly_name, language, variables, content_type, body, buttons, approval_status, approval_category, rejection_reason}`.

### 2. `apps/acolhimento/views.py` — endpoint AJAX
- `TemplatePreviewView(LoginRequiredMixin, MensagensPermissaoMixin, View)` — mesma permissão staff/super do disparo. `get(request)`:
  - `sid = request.GET.get('sid','').strip()`; se não começar com `HX` → `JsonResponse({ok:False, error:'SID inválido (deve começar com HX).'}, status=400)`;
  - `try: data = fetch_content_template(sid)` → `TwilioWhatsAppError` vira `JsonResponse({ok:False, error:str(e)})` (ex.: credenciais ausentes);
  - `exists=False` → `{ok:True, exists:False}`; senão devolve os campos do dict com `{ok:True, exists:True, ...}`.
- Importar `fetch_content_template` e `TwilioWhatsAppError` de `apps.acolhimento.twilio_service`.
- A **prévia renderizada** (substituir `{{1}}`, `{{2}}`… pelas variáveis digitadas, com `{nome}` → nome de exemplo) é feita **no JS** a partir do `body` cru + do campo `content_variables` — mantém o endpoint puro.

### 3. `apps/acolhimento/urls.py`
- `path('mensagens/template-preview/', TemplatePreviewView.as_view(), name='mensagens-template-preview')` + import.

### 4. `templates/mensagens_disparo_massa.html` — UI no bloco `data-fields-template`
Dentro de `.broadcast-template-grid`, ao lado de `#id_content_sid`: botão **"Pré-visualizar template"** (`type=button`, `data-preview-btn`, `data-no-loading`) e painel `<div data-preview-panel hidden>`.
- JS: no clique, lê `#id_content_sid` e `#id_content_variables`, faz `fetch('{% url 'mensagens-template-preview' %}?sid=...')`, e renderiza:
  - carregando / erro (credenciais ausentes → "Configure as credenciais Twilio para pré-visualizar");
  - `exists:false` → "SID não encontrado na sua conta Twilio";
  - sucesso → **badge de aprovação** (aprovado=verde / pendente=âmbar / rejeitado=vermelho + motivo), categoria, `friendly_name`, idioma; **corpo cadastrado** (cru) e **prévia renderizada** com as variáveis digitadas (bolha estilo WhatsApp); lista de **botões** e de **variáveis**.
- Reusar o padrão fetch→JSON já usado em `pessoa_mensagens.html` (`PrimeiroContatoMensagensMaisView`).

### 5. `static/css/pages.css`
Junto ao bloco `.broadcast-*`: `.template-preview` (painel), `.template-preview-bubble` (corpo estilo WhatsApp), `.approval-badge` + `--aprovado/--pendente/--rejeitado`. Usar variáveis do tema (`--primary`, `--accent-soft`, `--line`).

## Arquivos a modificar
- `apps/acolhimento/twilio_service.py` (funções novas)
- `apps/acolhimento/views.py` (`TemplatePreviewView` + imports)
- `apps/acolhimento/urls.py` (rota)
- `templates/mensagens_disparo_massa.html` (botão + painel + JS)
- `static/css/pages.css` (painel + badges)
- `apps/acolhimento/tests.py` (testes)

## Reúso
- `_load_twilio_sdk()` / padrão `_build_client()` em `twilio_service.py`.
- `MensagensPermissaoMixin` (staff/super) em `views.py`.
- Convenção `metadata_envio['twilio_template']['content_sid']` já usada no envio (`fila_processor.py`).
- Padrão fetch→JSON do JS em `pessoa_mensagens.html`.

## Verificação
- **Testes** (`apps/acolhimento/tests.py`), com `unittest.mock.patch('apps.acolhimento.views.fetch_content_template')` para não bater na rede:
  - sucesso → 200 JSON com `exists:True`, `body`, `approval_status`;
  - SID `HX` inexistente (mock retorna `{'exists': False}`) → `{ok:True, exists:False}`;
  - SID inválido (sem `HX`) → 400;
  - `TwilioWhatsAppError` (credenciais ausentes) → `{ok:False, error:...}`;
  - permissão: usuário sem staff → 403 (via `RequestFactory` + `assertRaises(PermissionDenied)`, evitando render 403 sem staticfiles — mesmo padrão já usado nos testes atuais).
- **Manual** (servidor local, logado como admin, com `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN` reais): modo Template → colar um `HX...` real → "Pré-visualizar" → ver corpo + status; colar SID falso → "não encontrado"; sem credenciais → erro amigável. Conferir overflow/mobile do painel novo (viewport 375) como no disparo.
- **Nota:** o preview exige credenciais Twilio no ambiente; em dev sem credenciais, só o caminho de erro amigável é exercitável fora dos testes mockados.

## Fora de escopo (v1)
- Cache das respostas da Content API.
- Dropdown para listar/escolher templates (buscar todos via `client.content.v1.content_and_approvals.list()`).
- Preview de mídia/header de imagem.
- Preview no envio individual (tela de conversa).

## Snippets de referência (pesquisados, para acelerar a execução)
```python
# twilio_service.py
def fetch_content_template(content_sid: str) -> dict:
    sid = (content_sid or '').strip()
    if not sid:
        raise TwilioWhatsAppError('Informe o Content SID.')
    client, twilio_exc_class = _build_readonly_client()
    try:
        content = client.content.v1.contents(sid).fetch()
    except twilio_exc_class as exc:
        if getattr(exc, 'status', None) == 404:
            return {'exists': False, 'sid': sid}
        raise TwilioWhatsAppError(str(exc)) from exc
    body, buttons, tipo = _extrair_corpo_botoes(content.types or {})
    approval = {}
    try:
        ap = client.content.v1.contents(sid).approval_fetch().fetch()
        approval = ap.whatsapp or {}
    except twilio_exc_class:
        approval = {}
    return {
        'exists': True, 'sid': sid,
        'friendly_name': content.friendly_name, 'language': content.language,
        'variables': content.variables or {}, 'content_type': tipo,
        'body': body, 'buttons': buttons,
        'approval_status': approval.get('status'),
        'approval_category': approval.get('category'),
        'rejection_reason': approval.get('rejection_reason'),
    }
```
