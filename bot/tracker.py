"""Rastreadores genéricos (financeiro, exercícios) — mesma mecânica da dieta.

Um único banco guarda lançamentos de várias categorias (kind). Cada lançamento
tem uma descrição, um valor numérico opcional (R$, minutos...) e uma etiqueta
(tag) opcional pra agrupar (ex: 'alimentação', 'corrida').
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo(config.TIMEZONE)


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.TRACKER_DB), exist_ok=True)
    con = sqlite3.connect(config.TRACKER_DB)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            day TEXT NOT NULL,
            kind TEXT NOT NULL,          -- 'financeiro' | 'exercicio' | ...
            description TEXT NOT NULL,
            value REAL,                  -- R$, minutos, etc.
            unit TEXT,
            tag TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            category TEXT NOT NULL,
            month TEXT NOT NULL DEFAULT '',  -- 'AAAA-MM'; vazio = padrão recorrente
            limit_value REAL NOT NULL,
            UNIQUE(kind, category, month)
        )
        """
    )
    return con


def _brl(v: float) -> str:
    """Formata em Real (R$ 1.234,56)."""
    s = f"{v:,.2f}"
    s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"R$ {s}"


def _barra(pct: float, largura: int = 10) -> str:
    pct = max(0.0, min(pct, 1.5))  # deixa passar um pouco de 100% pra mostrar estouro
    cheio = min(int(round(pct * largura)), largura)
    return "█" * cheio + "░" * (largura - cheio)


def log_entry(
    kind: str,
    description: str,
    value: float | None = None,
    unit: str = "",
    tag: str = "",
    when_iso: str = "",
) -> str:
    dt = datetime.now(_TZ)
    if when_iso:
        dt = datetime.fromisoformat(when_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ)
    con = _conn()
    with con:
        con.execute(
            "INSERT INTO entries (ts, day, kind, description, value, unit, tag) "
            "VALUES (?,?,?,?,?,?,?)",
            (dt.isoformat(), dt.strftime("%Y-%m-%d"), kind, description, value, unit, tag),
        )
    con.close()
    val = f" — {value:g} {unit}".rstrip() if value is not None else ""
    etq = f" [{tag}]" if tag else ""
    return f"Registrado ({dt.strftime('%d/%m %H:%M')}): {description}{val}{etq}"


def _range(period: str, start_date: str, end_date: str) -> tuple[str, str, str]:
    hoje = datetime.now(_TZ).date()
    if start_date or end_date:
        ini = start_date or end_date
        fim = end_date or start_date
        return ini, fim, f"{ini} a {fim}"
    if period == "day":
        return hoje.isoformat(), hoje.isoformat(), "hoje"
    if period == "week":
        seg = hoje - timedelta(days=hoje.weekday())
        return seg.isoformat(), hoje.isoformat(), "esta semana"
    if period == "month":
        return hoje.replace(day=1).isoformat(), hoje.isoformat(), "este mês"
    return hoje.replace(day=1).isoformat(), hoje.isoformat(), "este mês"


def budget_set(kind: str, category: str, limit_value: float, month: str = "") -> str:
    con = _conn()
    with con:
        con.execute(
            "INSERT INTO budgets (kind, category, month, limit_value) VALUES (?,?,?,?) "
            "ON CONFLICT(kind, category, month) DO UPDATE SET limit_value=excluded.limit_value",
            (kind, category, month, limit_value),
        )
    con.close()
    alvo = f"em {month}" if month else "todo mês (padrão)"
    return f"Orçamento de {category} definido: {_brl(limit_value)} {alvo}."


def budget_list(kind: str) -> str:
    con = _conn()
    rows = con.execute(
        "SELECT category, month, limit_value FROM budgets WHERE kind=? "
        "ORDER BY category, month",
        (kind,),
    ).fetchall()
    con.close()
    if not rows:
        return "Nenhum orçamento definido ainda."
    linhas = ["Orçamentos:"]
    for cat, mes, lim in rows:
        alvo = mes if mes else "todo mês (padrão)"
        linhas.append(f"  • {cat}: {_brl(lim)} — {alvo}")
    return "\n".join(linhas)


def _budget_for(kind: str, category: str, month: str) -> float | None:
    con = _conn()
    row = con.execute(
        "SELECT limit_value FROM budgets WHERE kind=? AND category=? AND month=?",
        (kind, category, month),
    ).fetchone()
    if row is None:
        row = con.execute(
            "SELECT limit_value FROM budgets WHERE kind=? AND category=? AND month=''",
            (kind, category),
        ).fetchone()
    con.close()
    return row[0] if row else None


def summary(
    kind: str, period: str = "month", start_date: str = "", end_date: str = ""
) -> str:
    ini, fim, rotulo = _range(period, start_date, end_date)
    con = _conn()
    rows = con.execute(
        "SELECT day, description, value, unit, tag, ts, id FROM entries "
        "WHERE kind=? AND day>=? AND day<=? ORDER BY ts",
        (kind, ini, fim),
    ).fetchall()
    con.close()

    if not rows:
        return f"Nada registrado em {kind} para {rotulo} ({ini} a {fim})."

    total = sum(r[2] or 0 for r in rows)
    unit = next((r[3] for r in rows if r[3]), "")
    moeda = unit == "R$"
    fmt = _brl if moeda else (lambda v: f"{v:g} {unit}".rstrip())

    por_tag: dict[str, float] = {}
    for r in rows:
        por_tag[r[4] or "(sem etiqueta)"] = por_tag.get(r[4] or "(sem etiqueta)", 0) + (r[2] or 0)

    # Orçamento só faz sentido comparado a um mês cheio — usa o mês do fim do período
    mes_ref = fim[:7]

    linhas = [f"*{kind.capitalize()} — {rotulo}* ({ini} a {fim})", f"Total: *{fmt(total)}*"]
    if len(por_tag) > 1 or (por_tag and list(por_tag)[0] != "(sem etiqueta)"):
        linhas.append("")
        linhas.append("Por categoria:")
        for tag, v in sorted(por_tag.items(), key=lambda x: -x[1]):
            pct_total = (v / total * 100) if total else 0
            trecho = f"  • {tag}: {fmt(v)} ({pct_total:.0f}% do total)"
            if moeda:
                limite = _budget_for(kind, tag, mes_ref)
                if limite:
                    pct_orc = v / limite if limite else 0
                    aviso = " ⚠️" if pct_orc > 1 else ""
                    trecho += (
                        f"\n     orçamento: {_barra(pct_orc)} {pct_orc*100:.0f}% de "
                        f"{_brl(limite)}{aviso}"
                    )
            linhas.append(trecho)

    MOSTRAR_ATE = 15
    linhas.append("")
    if len(rows) <= MOSTRAR_ATE:
        linhas.append(f"Lançamentos ({len(rows)}):")
        for r in rows:
            val = f" — {fmt(r[2])}" if r[2] is not None else ""
            etq = f" [{r[4]}]" if r[4] else ""
            linhas.append(f"  • #{r[6]} {r[0]} {r[1]}{val}{etq}")
    else:
        linhas.append(
            f"({len(rows)} lançamentos no período — resumido por categoria acima; "
            "peça 'lista completa' se quiser ver um por um)"
        )
    return "\n".join(linhas)


def list_raw(kind: str, period: str = "month", start_date: str = "", end_date: str = "") -> str:
    """Lista TODOS os lançamentos do período, sem resumir — usa quando o Állan
    pedir a lista completa, item por item (ex: pra conferir uma fatura)."""
    ini, fim, rotulo = _range(period, start_date, end_date)
    con = _conn()
    rows = con.execute(
        "SELECT day, description, value, unit, tag, ts, id FROM entries "
        "WHERE kind=? AND day>=? AND day<=? ORDER BY ts",
        (kind, ini, fim),
    ).fetchall()
    con.close()
    if not rows:
        return f"Nada registrado em {kind} para {rotulo} ({ini} a {fim})."
    moeda = any(r[3] == "R$" for r in rows)
    fmt = _brl if moeda else (lambda v: f"{v:g}")
    linhas = [f"{kind.capitalize()} — {rotulo} — {len(rows)} lançamentos:"]
    for r in rows:
        val = f" — {fmt(r[2])} {'' if moeda else (r[3] or '')}".rstrip() if r[2] is not None else ""
        etq = f" [{r[4]}]" if r[4] else ""
        linhas.append(f"  • #{r[6]} {r[0]} {r[1]}{val}{etq}")
    return "\n".join(linhas)


def delete_entry(entry_id: int) -> str:
    con = _conn()
    with con:
        cur = con.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    con.close()
    return (
        f"Lançamento #{entry_id} removido."
        if cur.rowcount
        else f"Não achei o lançamento #{entry_id}."
    )


def edit_entry(
    entry_id: int,
    description: str | None = None,
    value: float | None = None,
    tag: str | None = None,
    when_iso: str = "",
) -> str:
    campos, vals = [], []
    if description is not None:
        campos.append("description=?")
        vals.append(description)
    if value is not None:
        campos.append("value=?")
        vals.append(value)
    if tag is not None:
        campos.append("tag=?")
        vals.append(tag)
    if when_iso:
        dt = datetime.fromisoformat(when_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ)
        campos.append("ts=?")
        vals.append(dt.isoformat())
        campos.append("day=?")
        vals.append(dt.strftime("%Y-%m-%d"))
    if not campos:
        return "Nada para editar."
    vals.append(entry_id)
    con = _conn()
    with con:
        cur = con.execute(f"UPDATE entries SET {', '.join(campos)} WHERE id=?", vals)
    con.close()
    return (
        f"Lançamento #{entry_id} atualizado."
        if cur.rowcount
        else f"Não achei o lançamento #{entry_id}."
    )


# ---------------------------------------------------------------------------
# Esquemas para o Claude — financeiro e exercícios
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "fin_log",
        "description": (
            "Registra um GASTO/entrada no financeiro. amount em reais (positivo = "
            "gasto). category opcional (ex: alimentação, transporte, lazer, contas)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "amount": {"type": "number", "description": "valor em R$"},
                "category": {"type": "string"},
                "when_iso": {"type": "string"},
            },
            "required": ["description", "amount"],
        },
    },
    {
        "name": "fin_summary",
        "description": "Resumo financeiro por período (day/week/month) ou start_date/end_date. Total e por categoria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["day", "week", "month"]},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
    },
    {
        "name": "fin_list",
        "description": (
            "Lista TODOS os lançamentos financeiros do período, um por um, sem "
            "resumir por categoria. Use quando o Állan pedir a lista completa/"
            "item por item (ex: conferir uma fatura linha a linha) — NÃO tente "
            "recalcular ou re-somar isso na mão, os totais certos já vêm prontos "
            "no fin_summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["day", "week", "month"]},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
    },
    {
        "name": "fin_budget_set",
        "description": (
            "Define/atualiza uma meta de orçamento mensal para uma categoria "
            "financeira (ex: 'lazer: R$800/mês'). Use quando o Állan definir um "
            "limite de gasto para planejamento financeiro. Se month vier vazio, "
            "vale como padrão recorrente para todo mês sem meta específica."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string"},
                "limit": {"type": "number", "description": "limite em R$"},
                "month": {"type": "string", "description": "AAAA-MM; vazio = padrão recorrente"},
            },
            "required": ["category", "limit"],
        },
    },
    {
        "name": "fin_budget_list",
        "description": "Lista as metas de orçamento financeiro já definidas.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "ex_log",
        "description": (
            "Registra um TREINO/exercício. minutes opcional (duração). type opcional "
            "(ex: corrida, musculação, futebol). Detalhes (séries/pesos/distância) vão na description."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "minutes": {"type": "number"},
                "type": {"type": "string"},
                "when_iso": {"type": "string"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "ex_summary",
        "description": "Resumo dos treinos por período (day/week/month) ou start_date/end_date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["day", "week", "month"]},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
            },
        },
    },
    {
        "name": "entry_delete",
        "description": (
            "Apaga um lançamento de financeiro OU exercício pelo id (veja o #id no "
            "fin_summary/ex_summary)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"entry_id": {"type": "integer"}},
            "required": ["entry_id"],
        },
    },
    {
        "name": "entry_edit",
        "description": (
            "Corrige um lançamento de financeiro OU exercício pelo id. Campos: description, "
            "value (R$ no financeiro / minutos no exercício), tag (categoria/tipo), when_iso."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "integer"},
                "description": {"type": "string"},
                "value": {"type": "number"},
                "tag": {"type": "string"},
                "when_iso": {"type": "string"},
            },
            "required": ["entry_id"],
        },
    },
]

DISPATCH = {
    "fin_log": lambda a: log_entry(
        "financeiro", a["description"], a["amount"], "R$", a.get("category", ""), a.get("when_iso", "")
    ),
    "fin_summary": lambda a: summary(
        "financeiro", a.get("period", "month"), a.get("start_date", ""), a.get("end_date", "")
    ),
    "fin_list": lambda a: list_raw(
        "financeiro", a.get("period", "month"), a.get("start_date", ""), a.get("end_date", "")
    ),
    "fin_budget_set": lambda a: budget_set(
        "financeiro", a["category"], a["limit"], a.get("month", "")
    ),
    "fin_budget_list": lambda a: budget_list("financeiro"),
    "ex_log": lambda a: log_entry(
        "exercicio", a["description"], a.get("minutes"), "min", a.get("type", ""), a.get("when_iso", "")
    ),
    "ex_summary": lambda a: summary(
        "exercicio", a.get("period", "week"), a.get("start_date", ""), a.get("end_date", "")
    ),
    "entry_delete": lambda a: delete_entry(a["entry_id"]),
    "entry_edit": lambda a: edit_entry(
        a["entry_id"],
        a.get("description"),
        a.get("value"),
        a.get("tag"),
        a.get("when_iso", ""),
    ),
}
