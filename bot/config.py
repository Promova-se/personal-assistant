"""Carrega configuração e segredos a partir de arquivos .env locais."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Raiz do projeto (a pasta que contém a pasta bot/)
ROOT = Path(__file__).resolve().parent.parent

# Carrega .env (segredos do bot) e .trello.env (credenciais do Trello)
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".trello.env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# Modelo do Claude. Opus 5 por padrão; troque para claude-sonnet-5 se quiser
# reduzir custo por mensagem.
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5").strip()

# Só responde a estes IDs de chat do Telegram (separados por vírgula).
# Deixe vazio para descobrir seu ID: o bot vai te dizer qual é no primeiro
# contato, aí você cola aqui.
_allowed = os.getenv("ALLOWED_CHAT_IDS", "").strip()
ALLOWED_CHAT_IDS = {
    int(x) for x in _allowed.replace(" ", "").split(",") if x
}

# Credenciais do Trello (já preenchidas em .trello.env)
TRELLO_KEY = os.getenv("TRELLO_KEY", "").strip()
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN", "").strip()

# Fuso do usuário (para datas/horários)
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo").strip()


def missing() -> list[str]:
    """Retorna a lista de segredos obrigatórios que ainda faltam."""
    faltando = []
    if not TELEGRAM_TOKEN:
        faltando.append("TELEGRAM_TOKEN")
    if not ANTHROPIC_API_KEY:
        faltando.append("ANTHROPIC_API_KEY")
    if not (TRELLO_KEY and TRELLO_TOKEN):
        faltando.append("TRELLO_KEY/TRELLO_TOKEN")
    return faltando
