"""Bot de Telegram (polling) que conversa com o Claude + Trello.

Rode com:  python -m bot.main
Funciona atrás de qualquer rede (usa polling, não precisa de IP público).
"""
from __future__ import annotations

import asyncio
import logging
import re

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import agent, config, costs, reminders, transcribe, tts, uploads

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("assistente")


async def _responder(update: Update, resposta: str, quer_audio: bool = False) -> None:
    """Manda a resposta como voz (se pedido e configurado) ou como texto."""
    if quer_audio and config.AUDIO_ENABLED:
        try:
            audio = await asyncio.to_thread(tts.synthesize, resposta)
            costs.record_openai_tts(len(resposta))
            await update.message.reply_voice(voice=audio)
            return
        except Exception:  # noqa: BLE001
            log.exception("Erro ao sintetizar áudio; caindo para texto")
    for pedaco in _quebrar(resposta, 4000):
        try:
            await update.message.reply_text(_md_telegram(pedaco), parse_mode="Markdown")
        except BadRequest:
            # Markdown desbalanceado (raro, ex: corte no meio de um "**"): manda
            # em texto puro em vez de falhar silenciosamente.
            log.warning("Markdown inválido, mandando em texto puro")
            await update.message.reply_text(_sem_markdown(pedaco))


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
        resposta, quer_audio = await asyncio.to_thread(agent.handle_message, chat_id, texto)
    except Exception as e:  # noqa: BLE001
        log.exception("Erro ao processar mensagem")
        resposta, quer_audio = f"Deu erro aqui: {e}", False

    await _responder(update, resposta, quer_audio)


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

    # Salva pra poder ser revisitada depois (files_list/files_view)
    try:
        await asyncio.to_thread(uploads.save, chat_id, dados, "image/jpeg", legenda, "photo", "jpg")
    except Exception:  # noqa: BLE001
        log.exception("Falha ao salvar a foto para revisitar depois")

    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        resposta, quer_audio = await asyncio.to_thread(
            agent.handle_message, chat_id, legenda, dados, "image/jpeg"
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Erro ao processar imagem")
        resposta, quer_audio = f"Deu erro ao ler a imagem: {e}", False

    await _responder(update, resposta, quer_audio)


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

    # contabiliza o custo do áudio (OpenAI) pela duração
    costs.record_openai_whisper(getattr(voz, "duration", 0) or 0)

    # Processa direto como se fosse texto (sem devolver a transcrição)
    try:
        resposta, quer_audio = await asyncio.to_thread(agent.handle_message, chat_id, texto)
    except Exception as e:  # noqa: BLE001
        log.exception("Erro ao processar áudio transcrito")
        resposta, quer_audio = f"Deu erro: {e}", False

    await _responder(update, resposta, quer_audio)


def _extrair_texto(nome: str, dados: bytes) -> str | None:
    """Extrai texto de .txt/.csv/.md/.log/.json ou do .txt dentro de um .zip
    (export do WhatsApp). Retorna None se não souber ler."""
    low = nome.lower()
    if low.endswith(".zip"):
        import io
        import zipfile

        try:
            z = zipfile.ZipFile(io.BytesIO(dados))
            txts = [n for n in z.namelist() if n.lower().endswith(".txt")]
            if not txts:
                return None
            alvo = next((n for n in txts if "_chat" in n.lower()), txts[0])
            return z.read(alvo).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return None
    if low.endswith((".txt", ".csv", ".md", ".log", ".json", ".text")):
        return dados.decode("utf-8", errors="replace")
    # tenta como texto puro
    try:
        return dados.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _autorizado(chat_id):
        await _negar(update, chat_id)
        return

    doc = update.message.document
    nome = doc.file_name or "arquivo"
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "Arquivo grande demais (máx 20 MB pelo Telegram). "
            "No WhatsApp, exporte a conversa como 'Sem mídia'."
        )
        return

    arquivo = await doc.get_file()
    dados = bytes(await arquivo.download_as_bytearray())
    legenda = update.message.caption or ""

    # PDF: o Claude lê o arquivo nativamente (não tentamos extrair texto nós
    # mesmos — falharia, é binário). Funciona até com PDF escaneado/imagem.
    if nome.lower().endswith(".pdf") or doc.mime_type == "application/pdf":
        try:
            await asyncio.to_thread(uploads.save, chat_id, dados, "application/pdf", legenda or nome, "pdf", "pdf")
        except Exception:  # noqa: BLE001
            log.exception("Falha ao salvar o PDF para revisitar depois")

        await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            resposta, quer_audio = await asyncio.to_thread(
                agent.handle_pdf, chat_id, nome, dados, legenda
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Erro ao processar PDF")
            resposta, quer_audio = f"Deu erro ao analisar o PDF: {e}", False
        await _responder(update, resposta, quer_audio)
        return

    texto = _extrair_texto(nome, dados)
    if not texto:
        await update.message.reply_text(
            "Não consegui ler esse arquivo. Envie um .txt (export do WhatsApp) ou .zip."
        )
        return

    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # Salva o TEXTO já extraído (não o zip cru) pra poder ser revisitado depois
    try:
        await asyncio.to_thread(
            uploads.save, chat_id, texto.encode("utf-8"), "text/plain", legenda or nome, "document", "txt"
        )
    except Exception:  # noqa: BLE001
        log.exception("Falha ao salvar o documento para revisitar depois")
    try:
        resposta, quer_audio = await asyncio.to_thread(
            agent.handle_document, chat_id, nome, texto, legenda
        )
    except Exception as e:  # noqa: BLE001
        log.exception("Erro ao processar documento")
        resposta, quer_audio = f"Deu erro ao analisar o documento: {e}", False

    await _responder(update, resposta, quer_audio)


async def _reminders_loop(app: Application) -> None:
    """Roda em segundo plano: a cada ~20s, manda os lembretes que venceram."""
    while True:
        try:
            for rid, chat_id, message in await asyncio.to_thread(reminders.due_now):
                try:
                    await app.bot.send_message(chat_id=chat_id, text=f"⏰ {message}")
                except Exception:  # noqa: BLE001
                    log.exception("Falha ao enviar lembrete #%s", rid)
                await asyncio.to_thread(reminders.complete, rid)
        except asyncio.CancelledError:
            break  # encerramento normal (deploy/restart) — sai sem barulho
        except Exception:  # noqa: BLE001
            log.exception("Erro no laço de lembretes")
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            break


async def _cleanup_loop(app: Application) -> None:
    """Roda em segundo plano: uma vez por dia, apaga arquivos/fotos com mais de
    90 dias (disco + índice) para não acumular dado sensível indefinidamente."""
    while True:
        try:
            n = await asyncio.to_thread(uploads.cleanup_old, 90)
            if n:
                log.info("Limpeza: %d arquivo(s) com mais de 90 dias removido(s)", n)
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            log.exception("Erro na limpeza de arquivos antigos")
        try:
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break


async def _post_init(app: Application) -> None:
    app.create_task(_reminders_loop(app), update=None)
    app.create_task(_cleanup_loop(app), update=None)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra qualquer erro não tratado e avisa o usuário (sem travar o bot)."""
    log.exception("Erro não tratado no handler", exc_info=ctx.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "Tive um problema ao processar isso. Pode tentar de novo?"
            )
    except Exception:  # noqa: BLE001
        pass


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


def _md_telegram(texto: str) -> str:
    """Converte o markdown 'de chat' que o Claude escreve (**negrito**, títulos
    com #, listas com '- ') para o que o Telegram entende (parse_mode=Markdown,
    versão legada: *negrito* com um asterisco só)."""
    texto = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", texto, flags=re.MULTILINE)
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto, flags=re.DOTALL)
    texto = re.sub(r"^[-*]\s+", "• ", texto, flags=re.MULTILINE)
    return texto


def _sem_markdown(texto: str) -> str:
    """Tira toda marcação de markdown — usado como fallback em texto puro."""
    texto = re.sub(r"^#{1,6}\s+", "", texto, flags=re.MULTILINE)
    texto = re.sub(r"\*\*(.+?)\*\*", r"\1", texto, flags=re.DOTALL)
    texto = re.sub(r"[*_`]", "", texto)
    texto = re.sub(r"^[-]\s+", "• ", texto, flags=re.MULTILINE)
    return texto


def main() -> None:
    faltando = config.missing()
    if faltando:
        raise SystemExit(
            "Faltam segredos no .env / .trello.env: " + ", ".join(faltando)
        )

    app = (
        Application.builder()
        .token(config.TELEGRAM_TOKEN)
        .concurrent_updates(4)  # uma mensagem travada não bloqueia as outras
        .post_init(_post_init)  # inicia o laço de lembretes em segundo plano
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_error_handler(on_error)

    log.info("Assistente no ar (modelo=%s). Ctrl+C para parar.", config.MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
