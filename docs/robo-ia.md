# Robô com IA (texto livre e áudios)

O robô de menu continua igual para **número** e **mensagem curta com palavra-chave**. Com a IA ligada:

- **Frase sem opção** ou **frase longa** (mais de 4 palavras): a IA (Ollama Cloud) lê a conversa e decide:
  - **responder** só com a *base de conhecimento* e as respostas das opções;
  - escolher uma **opção** do menu (sai a resposta oficial dela, com arquivo);
  - **transferir** para a equipe (assunto pastoral/sensível, pedido de oração ou visita, quando não sabe).
- **Áudio**: é transcrito depois do download e tratado como texto. A transcrição aparece na conversa, inclusive para quem já está com a equipe.
- **Falhas** (sem chave, erro, limite, resposta ilegível): tenta a palavra-chave e, sem ela, manda o "não entendi" + menu de sempre.
- **Limite por pessoa**: passando de N respostas da IA em 24h, a conversa vai para a equipe.

As respostas da IA aparecem na conversa com a etiqueta **"Respondida pela IA"**. Os tokens gastos ficam em `metadata_envio['ia']`.

## Serviços

| O quê | Serviço | Custo |
|---|---|---|
| Texto | Ollama Cloud (`POST https://ollama.com/api/chat`), modelo padrão `gemma4:31b` | pago por uso; ~US$ 0,0005 por resposta |
| Áudio | Groq Whisper (`whisper-large-v3-turbo`) | plano grátis (limite por hora/dia) |
| Áudio (reserva) | faster-whisper `small` no próprio servidor | grátis; usado quando a Groq dá 429, 5xx ou está fora do ar |

A reserva local transcreve **um áudio por vez** no servidor (trava em arquivo) e recusa áudios acima de `TRANSCRICAO_LOCAL_MAX_SEGUNDOS`. Os pesos (~460 MB) ficam no volume `whisper_modelos`.

## Configuração

1. Crie a chave em <https://ollama.com/settings/keys> e coloque créditos (pago por uso).
2. Crie a chave em <https://console.groq.com/keys>.
3. No `.env` do servidor: `OLLAMA_API_KEY`, `GROQ_API_KEY` (opcionais: `OLLAMA_MODELO`, `TRANSCRICAO_LOCAL_*`).
4. Deploy e, uma vez, baixe o modelo local:
   ```bash
   docker exec pibvp-acolhimento-web python manage.py preparar_whisper
   ```
5. Em **Configurações > Atendimento automático > Inteligência artificial**: preencha a base de conhecimento, use **Testar a IA** e ligue "IA ligada" e "Transcrever áudios recebidos".

## Privacidade

Vão para a Ollama: as regras, a base de conhecimento, as opções, o **primeiro nome** e as últimas 10 mensagens da conversa (sem telefone). A Ollama declara não guardar nem treinar com os dados. O áudio vai para a Groq; na reserva, não sai do servidor.
