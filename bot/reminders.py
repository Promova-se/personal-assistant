"""Lembretes/mensagens programadas.

O Claude cria um lembrete com data/hora (ISO 8601); um laço em segundo plano
(no main.py) verifica a cada ~20s se algum venceu e manda a mensagem sozinho,
sem precisar do usuário perguntar nada.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo(config.TIMEZONE)
_DB = os.path.join(os.path.dirname(config.DIET_DB), "reminders.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    con = sqlite3.connect(_DB)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            due_ts TEXT NOT NULL,     -- ISO 8601 com fuso
            created_ts TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return con


def create(chat_id: int, message: str, when_iso: str) -> str:
    dt = datetime.fromisoformat(when_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    agora = datetime.now(_TZ)
    if dt <= agora:
        return "Esse horário já passou. Me dê uma data/hora no futuro."
    con = _conn()
    with con:
        cur = con.execute(
            "INSERT INTO reminders (chat_id, message, due_ts, created_ts) VALUES (?,?,?,?)",
            (chat_id, message, dt.isoformat(), agora.isoformat()),
        )
        rid = cur.lastrowid
    con.close()
    return f"Lembrete #{rid} agendado para {dt.strftime('%d/%m %H:%M')}: {message}"


def list_pending(chat_id: int) -> str:
    con = _conn()
    rows = con.execute(
        "SELECT id, message, due_ts FROM reminders WHERE chat_id=? AND sent=0 ORDER BY due_ts",
        (chat_id,),
    ).fetchall()
    con.close()
    if not rows:
        return "Nenhum lembrete pendente."
    linhas = ["Lembretes pendentes:"]
    for rid, msg, due in rows:
        try:
            dt = datetime.fromisoformat(due).strftime("%d/%m %H:%M")
        except ValueError:
            dt = due
        linhas.append(f"  • #{rid} {dt} — {msg}")
    return "\n".join(linhas)


def cancel(reminder_id: int) -> str:
    con = _conn()
    with con:
        cur = con.execute(
            "DELETE FROM reminders WHERE id=? AND sent=0", (reminder_id,)
        )
    con.close()
    return (
        f"Lembrete #{reminder_id} cancelado."
        if cur.rowcount
        else f"Não achei um lembrete pendente #{reminder_id}."
    )


def due_now() -> list[tuple[int, int, str]]:
    """Lembretes vencidos e ainda não enviados: (id, chat_id, message)."""
    agora = datetime.now(_TZ).isoformat()
    con = _conn()
    rows = con.execute(
        "SELECT id, chat_id, message FROM reminders WHERE sent=0 AND due_ts<=?",
        (agora,),
    ).fetchall()
    con.close()
    return rows


def mark_sent(reminder_id: int) -> None:
    con = _conn()
    with con:
        con.execute("UPDATE reminders SET sent=1 WHERE id=?", (reminder_id,))
    con.close()


# ---------------------------------------------------------------------------
# Esquemas para o Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "reminder_create",
        "description": (
            "Agenda um lembrete/mensagem programada. O bot vai mandar essa mensagem "
            "sozinho na data/hora indicada (when_iso, ISO 8601 com fuso, ex: "
            "2026-08-20T09:00:00-03:00). Use para 'me lembra de X às Y', 'daqui a 30 "
            "minutos me avisa Z', 'todo dia' (crie um por enquanto e avise que recorrência "
            "ainda não é suportada)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "O que enviar no lembrete"},
                "when_iso": {"type": "string"},
            },
            "required": ["message", "when_iso"],
        },
    },
    {
        "name": "reminder_list",
        "description": "Lista os lembretes pendentes (ainda não enviados).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reminder_cancel",
        "description": "Cancela um lembrete pendente pelo id (veja o #id em reminder_list).",
        "input_schema": {
            "type": "object",
            "properties": {"reminder_id": {"type": "integer"}},
            "required": ["reminder_id"],
        },
    },
]

DISPATCH = {
    "reminder_create": lambda a: create(a["_chat_id"], a["message"], a["when_iso"]),
    "reminder_list": lambda a: list_pending(a["_chat_id"]),
    "reminder_cancel": lambda a: cancel(a["reminder_id"]),
}
