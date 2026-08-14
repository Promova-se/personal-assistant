"""Registro de refeições e relatórios da dieta (SQLite local).

O Claude estima calorias/macros a partir da foto ou do texto e chama
diet_log_meal para registrar. Depois, diet_summary devolve os totais por
período pra ele resumir.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo(config.TIMEZONE)


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DIET_DB), exist_ok=True)
    con = sqlite3.connect(config.DIET_DB)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,               -- ISO local (com fuso)
            day TEXT NOT NULL,              -- YYYY-MM-DD local (para agrupar)
            description TEXT NOT NULL,
            calories INTEGER,
            protein REAL,
            carbs REAL,
            fat REAL
        )
        """
    )
    return con


def log_meal(
    description: str,
    calories: int | None = None,
    protein: float | None = None,
    carbs: float | None = None,
    fat: float | None = None,
    when_iso: str = "",
) -> str:
    if when_iso:
        dt = datetime.fromisoformat(when_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ)
    else:
        dt = datetime.now(_TZ)
    con = _conn()
    with con:
        con.execute(
            "INSERT INTO meals (ts, day, description, calories, protein, carbs, fat) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                dt.isoformat(),
                dt.strftime("%Y-%m-%d"),
                description,
                calories,
                protein,
                carbs,
                fat,
            ),
        )
    con.close()
    cal = f"{calories} kcal" if calories is not None else "kcal não estimada"
    return f"Refeição registrada ({dt.strftime('%d/%m %H:%M')}): {description} — {cal}"


def _range(period: str, start_date: str, end_date: str) -> tuple[str, str, str]:
    hoje = datetime.now(_TZ).date()
    if start_date or end_date:
        ini = start_date or end_date
        fim = end_date or start_date
        return ini, fim, f"{ini} a {fim}"
    if period == "day":
        return hoje.isoformat(), hoje.isoformat(), "hoje"
    if period == "week":
        seg = hoje - timedelta(days=hoje.weekday())  # segunda desta semana
        return seg.isoformat(), hoje.isoformat(), "esta semana"
    if period == "month":
        primeiro = hoje.replace(day=1)
        return primeiro.isoformat(), hoje.isoformat(), "este mês"
    # padrão: hoje
    return hoje.isoformat(), hoje.isoformat(), "hoje"


def summary(period: str = "day", start_date: str = "", end_date: str = "") -> str:
    ini, fim, rotulo = _range(period, start_date, end_date)
    con = _conn()
    rows = con.execute(
        "SELECT day, description, calories, protein, carbs, fat, ts "
        "FROM meals WHERE day >= ? AND day <= ? ORDER BY ts",
        (ini, fim),
    ).fetchall()
    con.close()

    if not rows:
        return f"Nenhuma refeição registrada em {rotulo} ({ini} a {fim})."

    total_cal = sum(r[2] or 0 for r in rows)
    total_p = sum(r[3] or 0 for r in rows)
    total_c = sum(r[4] or 0 for r in rows)
    total_f = sum(r[5] or 0 for r in rows)

    # Agrupa por dia
    por_dia: dict[str, list] = {}
    for r in rows:
        por_dia.setdefault(r[0], []).append(r)

    linhas = [f"Dieta — {rotulo} ({ini} a {fim})"]
    for dia, itens in por_dia.items():
        cal_dia = sum(i[2] or 0 for i in itens)
        linhas.append(f"\n{dia} — {cal_dia} kcal")
        for i in itens:
            hora = i[6][11:16] if len(i[6]) >= 16 else ""
            cal = f"{i[2]} kcal" if i[2] is not None else "?"
            linhas.append(f"  • {hora} {i[1]} — {cal}")

    n_dias = len(por_dia)
    media = round(total_cal / n_dias) if n_dias else 0
    linhas.append(
        f"\nTOTAL: {total_cal} kcal | P {round(total_p)}g · C {round(total_c)}g · G {round(total_f)}g"
    )
    if n_dias > 1:
        linhas.append(f"Média/dia: {media} kcal ({n_dias} dias)")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Esquemas para o Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "diet_log_meal",
        "description": (
            "Registra uma refeição no diário da dieta. Estime calories (kcal) e, se "
            "possível, protein/carbs/fat em gramas, a partir da foto ou descrição. "
            "when_iso opcional (ISO 8601) se não for agora."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "O que foi comido"},
                "calories": {"type": "integer"},
                "protein": {"type": "number", "description": "proteína (g)"},
                "carbs": {"type": "number", "description": "carboidrato (g)"},
                "fat": {"type": "number", "description": "gordura (g)"},
                "when_iso": {"type": "string"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "diet_summary",
        "description": (
            "Resumo da dieta por período. period = 'day' (hoje), 'week' (esta semana) "
            "ou 'month' (este mês). Ou passe start_date/end_date (YYYY-MM-DD) para um "
            "intervalo específico. Traz total de kcal, macros e detalhamento por dia."
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
]

DISPATCH = {
    "diet_log_meal": lambda a: log_meal(
        a["description"],
        a.get("calories"),
        a.get("protein"),
        a.get("carbs"),
        a.get("fat"),
        a.get("when_iso", ""),
    ),
    "diet_summary": lambda a: summary(
        a.get("period", "day"), a.get("start_date", ""), a.get("end_date", "")
    ),
}
