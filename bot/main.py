"""Bot de Telegram (polling) que conversa com o Claude + Trello.

Rode com:  python -m bot.main
Funciona atrás de qualquer rede (usa polling, não precisa de IP público).
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import agent, config, transcribe

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("assistente")


def _autorizado(chat_id: int) -> bool:
    # Se ninguém foi configurado ainda, bloqueia todo mundo (fail-safe) mas
    # o log mostra o ID para você adicionar em ALLOWED_CHAT_IDS.
    return bool(config.ALLOWED_CHAT_IDS) and chat_id in config.ALLOWED_CHAT_IDS


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _autorizado(chat_id):
        await _negar(update, chat_id)
        return
    await update.message.reply_text(
        "Oi! Sou seu assistente pessoal. 🤖\n"
        "Posso ver e mexer no seu Trello e te ajudar a organizar o dia.\n\n"
        "Exemplos:\n"
        "• o que vence hoje?\n"
        "• cria um card 'Ligar pro contador' pra amanhã\n"
        "• marca a tarefa X como concluída\n\n"
        "Use /reset para limpar a conversa."
    )


async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _autorizado(chat_id):
        await _negar(update, chat_id)
        return
    agent.reset(chat_id)
    await update.message.reply_text("Conversa limpa. 🧹")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _autorizado(chat_id):
        await _negar(update, chat_id)
        return

    texto = update.message.text or ""
    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        # agent.handle_message é bloqueante (SDK síncrono) -> roda em thread
        resposta = await asyncio.to_thread(agent.handle_message, chat_id, texto)
    except Exception as e:  # noqa: BLE001
        log.exception("Erro ao processar mensagem")
        resposta = f"Deu erro aqui: {e}"

    # Telegram limita mensagens a 4096 chars
    for pedaco in _quebrar(resposta, 4000):
        await update.message.reply_text(pedaco)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _autorizado(chat_id):
        await _negar(update, chat_id)
        return

    # Pega a maior resolução disponível
    foto = update.message.photo[-1]
    arquivo = await foto.get_file()
    dados = bytes(await arquivo.download_as_bytearray())
    legenda = update.message.caption or ""

    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        resposta = await asyncio.to_thread(
            agent.handle_message, chat_id, legenda, dados, "image/jpeg"
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Erro ao processar imagem")
        resposta = f"Deu erro ao ler a imagem: {e}"

    for pedaco in _quebrar(resposta, 4000):
        await update.message.reply_text(pedaco)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _autorizado(chat_id):
        await _negar(update, chat_id)
        return

    if not config.AUDIO_ENABLED:
        await update.message.reply_text(
            "Áudio ainda não está configurado (falta a chave da OpenAI)."
        )
        return

    voz = update.message.voice or update.message.audio
    arquivo = await voz.get_file()
    dados = bytes(await arquivo.download_as_bytearray())

    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        texto = await asyncio.to_thread(transcribe.transcribe, dados)
    except Exception as e:  # noqa: BLE001
        log.exception("Erro ao transcrever")
        await update.message.reply_text(f"Não consegui transcrever o áudio: {e}")
        return

    if not texto:
        await update.message.reply_text("Não entendi o áudio. Pode repetir?")
        return

    # Processa direto como se fosse texto (sem devolver a transcrição)
    try:
        resposta = await asyncio.to_thread(agent.handle_message, chat_id, texto)
    except Exception as e:  # noqa: BLE001
        log.exception("Erro ao processar áudio transcrito")
        resposta = f"Deu erro: {e}"

    for pedaco in _quebrar(resposta, 4000):
        await update.message.reply_text(pedaco)


async def _negar(update: Update, chat_id: int) -> None:
    log.warning("Chat não autorizado: %s", chat_id)
    await update.message.reply_text(
        "Você não está autorizado a usar este bot.\n"
        f"Se você é o dono, adicione este ID em ALLOWED_CHAT_IDS: {chat_id}"
    )


def _quebrar(texto: str, tamanho: int) -> list[str]:
    if not texto:
        return ["(vazio)"]
    return [texto[i : i + tamanho] for i in range(0, len(texto), tamanho)]


def main() -> None:
    faltando = config.missing()
    if faltando:
        raise SystemExit(
            "Faltam segredos no .env / .trello.env: " + ", ".join(faltando)
        )

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))

    log.info("Assistente no ar (modelo=%s). Ctrl+C para parar.", config.MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
