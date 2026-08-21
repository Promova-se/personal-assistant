"""Lembretes/mensagens programadas.

O Claude cria um lembrete com data/hora (ISO 8601); um laço em segundo plano
(no main.py) verifica a cada ~20s se algum venceu e manda a mensagem sozinho,
sem precisar do usuário perguntar nada.
"""
from __future__ import annotations

import calendar
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo(config.TIMEZONE)
_DB = os.path.join(os.path.dirname(config.DIET_DB), "reminders.db")

_RECUR_LABEL = {
    "none": "",
    "daily": " (todo dia)",
    "weekly": " (toda semana)",
    "monthly": " (todo mês)",
    "yearly": " (todo ano)",
}


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
            sent INTEGER NOT NULL DEFAULT 0,
            recurrence TEXT NOT NULL DEFAULT 'none'
        )
        """
    )
    # Coluna nova em bancos já existentes (não quebra instalações antigas)
    try:
        con.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'none'")
    except sqlite3.OperationalError:
        pass
    return con


def _add_interval(dt: datetime, recurrence: str) -> datetime:
    """Avança dt para a próxima ocorrência, tratando meses/anos com dias que
    não existem no mês alvo (ex: 29/fev em ano não bissexto -> 28/fev)."""
    if recurrence == "daily":
        return dt + timedelta(days=1)
    if recurrence == "weekly":
        return dt + timedelta(weeks=1)
    if recurrence == "monthly":
        mes = dt.month + 1
        ano = dt.year + (1 if mes > 12 else 0)
        mes = 1 if mes > 12 else mes
        dia = min(dt.day, calendar.monthrange(ano, mes)[1])
        return dt.replace(year=ano, month=mes, day=dia)
    if recurrence == "yearly":
        ano = dt.year + 1
        dia = min(dt.day, calendar.monthrange(ano, dt.month)[1])
        return dt.replace(year=ano, day=dia)
    return dt  # 'none' — não deveria chegar aqui


def create(chat_id: int, message: str, when_iso: str, recurrence: str = "none") -> str:
    if recurrence not in _RECUR_LABEL:
        recurrence = "none"
    dt = datetime.fromisoformat(when_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    agora = datetime.now(_TZ)
    if dt <= agora:
        return "Esse horário já passou. Me dê uma data/hora no futuro."
    con = _conn()
    with con:
        cur = con.execute(
            "INSERT INTO reminders (chat_id, message, due_ts, created_ts, recurrence) "
            "VALUES (?,?,?,?,?)",
            (chat_id, message, dt.isoformat(), agora.isoformat(), recurrence),
        )
        rid = cur.lastrowid
    con.close()
    rot = _RECUR_LABEL[recurrence]
    return f"Lembrete #{rid} agendado para {dt.strftime('%d/%m %H:%M')}{rot}: {message}"


def list_pending(chat_id: int) -> str:
    con = _conn()
    rows = con.execute(
        "SELECT id, message, due_ts, recurrence FROM reminders WHERE chat_id=? AND sent=0 "
        "ORDER BY due_ts",
        (chat_id,),
    ).fetchall()
    con.close()
    if not rows:
        return "Nenhum lembrete pendente."
    linhas = ["Lembretes pendentes:"]
    for rid, msg, due, rec in rows:
        try:
            dt = datetime.fromisoformat(due).strftime("%d/%m %H:%M")
        except ValueError:
            dt = due
        rot = _RECUR_LABEL.get(rec, "")
        linhas.append(f"  • #{rid} {dt}{rot} — {msg}")
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


def complete(reminder_id: int) -> None:
    """Chamado depois de enviar um lembrete: se for recorrente, reagenda para
    a próxima ocorrência; senão, marca como enviado (definitivo)."""
    con = _conn()
    row = con.execute(
        "SELECT due_ts, recurrence FROM reminders WHERE id=?", (reminder_id,)
    ).fetchone()
    if row is None:
        con.close()
        return
    due_ts, recurrence = row
    if recurrence and recurrence != "none":
        agora = datetime.now(_TZ)
        dt = datetime.fromisoformat(due_ts)
        # Avança até ultrapassar o momento atual (cobre o bot ter ficado
        # off-line por mais de um ciclo, ex: mais de um dia parado).
        while dt <= agora:
            dt = _add_interval(dt, recurrence)
        with con:
            con.execute(
                "UPDATE reminders SET due_ts=? WHERE id=?", (dt.isoformat(), reminder_id)
            )
    else:
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
            "minutos me avisa Z'. Para algo recorrente (aniversário, mensalidade, reunião "
            "semanal), passe recurrence: 'yearly' (aniversários), 'monthly', 'weekly' ou "
            "'daily'. when_iso é a PRIMEIRA ocorrência; as próximas são calculadas sozinhas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "O que enviar no lembrete"},
                "when_iso": {"type": "string"},
                "recurrence": {
                    "type": "string",
                    "enum": ["none", "daily", "weekly", "monthly", "yearly"],
                    "description": "Padrão: none (única vez).",
                },
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
    "reminder_create": lambda a: create(
        a["_chat_id"], a["message"], a["when_iso"], a.get("recurrence", "none")
    ),
    "reminder_list": lambda a: list_pending(a["_chat_id"]),
    "reminder_cancel": lambda a: cancel(a["reminder_id"]),
}
