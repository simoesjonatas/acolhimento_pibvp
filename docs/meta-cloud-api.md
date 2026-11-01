# WhatsApp pela API oficial da Meta (Cloud API)

Com `WHATSAPP_PROVIDER=meta` o sistema fala direto com o Graph API da Meta, sem
intermediario (Twilio) e sem WhatsApp Web pareado (Evolution). As regras de
opt-in, janela de 24h e templates aprovados valem integralmente: fora da janela
so sai template.

## O que muda em relacao aos outros providers

| | Twilio | Evolution (Baileys) | Meta (Cloud API) |
|---|---|---|---|
| Texto livre | so na janela de 24h | sempre | so na janela de 24h |
| Fora da janela | Content Template SID | texto simples | template aprovado (nome + idioma) |
| Variaveis do template | JSON `{"1": "{nome}"}` | `{nome}` no texto | mesmo JSON da Twilio |
| Webhook | `webhook/twilio-*` | `webhook/evolution/` | `webhook/meta/` (assinado com App Secret) |
| Pareamento | nao | QR code | nao (numero registrado no painel da Meta) |
| Anti-bloqueio (variacao de texto, "digitando") | nao | sim | nao se aplica |

## Variaveis de ambiente

| Variavel | De onde vem |
|---|---|
| `WHATSAPP_PROVIDER=meta` | liga o provider |
| `META_WA_PHONE_NUMBER_ID` | developers.facebook.com > app > WhatsApp > API Setup > Phone number ID |
| `META_WA_BUSINESS_ACCOUNT_ID` | mesmo lugar > WhatsApp Business Account ID (opcional; lista os templates na tela) |
| `META_WA_ACCESS_TOKEN` | business.facebook.com > Usuarios do sistema > token permanente com `whatsapp_business_messaging` e `whatsapp_business_management` |
| `META_WA_APP_SECRET` | developers.facebook.com > app > Configuracoes > Basico > Chave secreta do app |
| `META_WA_VERIFY_TOKEN` | string escolhida por nos; a mesma vai no painel ao cadastrar o webhook |
| `META_WA_WEBHOOK_URL` | `https://<dominio>/acolhimento/mensagens/webhook/meta/` (so informativo) |
| `META_WA_API_VERSION` | versao do Graph API (`v22.0`) |
| `META_TEMPLATE_OPT_IN_NOME` / `_IDIOMA` / `_VARIABLES` | template de primeiro contato aprovado |
| `META_TEMPLATE_CONTINUAR_NOME` / `_IDIOMA` / `_VARIABLES` | template de continuacao aprovado |
| `META_BOTAO_SIM` / `META_BOTAO_NAO` | texto exato dos botoes de resposta rapida aprovados (padrao `Sim, podemos` / `Agora não`) |

Os nomes e idiomas dos templates tambem podem ser definidos pela tela
**Configuracoes > Mensagens do WhatsApp** (o banco tem prioridade sobre o `.env`).

## Webhook

1. No painel da Meta (app > WhatsApp > Configuration > Webhook), informe a URL
   `https://<dominio>/acolhimento/mensagens/webhook/meta/` e o verify token.
   O app responde ao GET de verificacao com o `hub.challenge`.
2. Assine o campo **messages**. Ele traz tanto as mensagens recebidas quanto os
   status de entrega (sent / delivered / read / failed).
3. Todo POST chega assinado (`X-Hub-Signature-256`). Em producao, sem
   `META_WA_APP_SECRET` o webhook recusa tudo com 403 (ha um system check que avisa).
4. Toda mensagem recebida e marcada como lida na Meta (tique azul para a pessoa),
   sem travar o webhook se a chamada falhar. Quando o bot de menu responde e o
   processamento automatico da fila esta ligado, vai junto o "digitando...".

### Botoes de resposta rapida dos templates

O clique chega como `type=button` (`button.payload` / `button.text`) ou, em
mensagem interativa, `interactive.button_reply` (`id` / `title`). O texto e
comparado sem acento/caixa/pontuacao com `META_BOTAO_SIM` / `META_BOTAO_NAO`
(ids `sim` / `nao` tambem valem):

- **Sim, podemos**: a pessoa vai para *Em acompanhamento* (se ainda estava em
  Primeiro contato / Visitante), registra *Resposta recebida* na linha do tempo e
  o bot de menu **nao** e acionado. A entrada fica no retorno pendente para a
  equipe dar sequencia.
- **Agora nao**: registra *Resposta recebida* ("pediu para falar depois"), a
  entrada ja nasce vista pela equipe (fora do retorno pendente) e a tela da pessoa
  mostra o selo *Pediu para falar depois* ate ela escrever de novo.
- Outro botao: segue o fluxo normal (bot de menu com o texto do botao).

Em todos os casos a entrada guarda `meta_botao` (`sim` / `agora_nao` /
`desconhecido`) e `meta_botao_texto` em `metadata_resposta`, e a conversa mostra
"respondeu pelo botao: ...".

## Templates

- Nome: minusculas, numeros e `_`. Idioma: codigo da Meta (`pt_BR`).
- Variaveis: a `1` recebe sempre o nome da pessoa. Para outras, use o JSON de
  variaveis (`{"2": "Igreja {nome}"}`). Chaves numericas viram parametros
  posicionais (`{{1}}`, `{{2}}`); chaves com nome viram parametros nomeados
  (`{{nome}}`).
- Categoria: **Utility** para primeiro contato e continuacao; **Marketing** para
  disparo em massa (custa mais e exige opt-in).

## Tela de conexao

Em **Configuracoes > Conexao WhatsApp** (superusuario) a tela do modo Meta mostra:
numero, nome verificado e qualidade (vindos do Graph API), se o webhook tem
verify token e App Secret, a lista de templates com status de aprovacao e um
**envio de teste** (template de primeiro contato ou texto) para um numero digitado.

## Codigo

- `apps/acolhimento/meta_service.py`: cliente HTTP (texto, template, consultas).
- `apps/acolhimento/whatsapp_gateway.py`: `_enviar_meta` escolhe texto ou template.
- `apps/acolhimento/meta_webhooks.py`: verificacao, assinatura, entrada e status.
- `apps/acolhimento/meta_views.py`: pagina de configuracao e despachante da rota.
- `apps/acolhimento/template_config.py`: resolucao de nome/idioma/variaveis.
- `apps/acolhimento/tests_meta.py`: testes (sem rede).

## Erros comuns da Meta (traduzidos na tela)

| Codigo | Significado |
|---|---|
| 190 | token invalido/expirado (use token permanente de System User) |
| 131047 | fora da janela de 24h: envie template |
| 132001 | template nao existe / nao aprovado para esse nome+idioma |
| 132000 | numero de parametros diferente do template |
| 131026 | numero sem WhatsApp ou que bloqueou o contato |
| 131030 | numero nao esta na lista de teste (app ainda em modo de desenvolvimento) |
