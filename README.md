# Assistente Pessoal (Telegram + Claude + Trello)

Um bot de Telegram que conversa com o Claude e mexe no seu Trello para te
ajudar a organizar o dia.

## O que ele faz hoje
- Conversa geral (planejar, tirar dúvidas, pensar junto)
- **Trello**: listar quadros/listas/cards, ver o que vence, criar cards,
  mudar prazo, mover e concluir tarefas

> Google Agenda entra numa próxima fase (precisa de OAuth próprio do Google).

## Como ligar (uma vez)

1. **Instale as dependências** (dentro da pasta `personal-assistant`):
   ```bash
   pip install -r requirements.txt
   ```

2. **Crie o bot no Telegram**: fale com o **@BotFather**, mande `/newbot`,
   escolha um nome. Ele te dá um **token** (algo como `123456:ABC-...`).

3. **Pegue uma API key da Anthropic** em https://console.anthropic.com
   (aba *API Keys*) e adicione crédito.

4. **Preencha o arquivo `.env`** com o token do Telegram e a API key.
   (As credenciais do Trello já estão em `.trello.env`.)

5. **Rode o bot**:
   ```bash
   python -m bot.main
   ```

6. No Telegram, mande **/start** pro seu bot. Na primeira vez ele vai dizer
   "não autorizado" e mostrar o **seu ID de chat**. Copie esse número para
   `ALLOWED_CHAT_IDS` no `.env` e rode de novo. Pronto — só você usa o bot.

## Exemplos de uso
- *o que vence hoje?*
- *cria um card "Ligar pro contador" pra amanhã*
- *move a tarefa X pra lista Concluído*
- *me ajuda a planejar a tarde*

## Comandos
- `/start` — apresentação
- `/reset` — limpa a conversa

## Custo
Cada mensagem usa a API da Anthropic (centavos por mensagem). Para reduzir,
troque `CLAUDE_MODEL=claude-sonnet-5` no `.env`.

## Segurança
- `.env` e `.trello.env` guardam segredos e **não** vão pro git (ver `.gitignore`).
- O bot só responde aos IDs em `ALLOWED_CHAT_IDS`.
- Rodando no seu PC, o bot só funciona com o `python -m bot.main` aberto.
  Para 24h, dá pra migrar para um servidor depois.
