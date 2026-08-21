"""Contabiliza (estimando) o gasto em cada API a partir do uso real.

- Anthropic: calcula pelo número de tokens de cada resposta (entrada, saída,
  escrita/leitura de cache) e conta buscas web.
- OpenAI (Whisper): calcula pela duração do áudio.

É uma estimativa pelos preços atuais — próxima, mas pode diferir do painel oficial.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo(config.TIMEZONE)

# Preços em USD por 1 milhão de tokens (entrada, saída)
_RATES = {
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_SONNET5_INTRO = (2.0, 10.0)  # promoção até 2026-08-31
WHISPER_USD_PER_MIN = 0.006
WEB_SEARCH_USD_EACH = 0.01
TTS_USD_PER_CHAR = 15.0 / 1_000_000  # OpenAI tts-1


def _db() -> sqlite3.Connection:
    path = os.path.join(os.path.dirname(config.DIET_DB), "costs.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            day TEXT NOT NULL,
            api TEXT NOT NULL,      -- 'anthropic' | 'openai'
            usd REAL NOT NULL
        )
        """
    )
    return con


def _add(api: str, usd: float) -> None:
    if usd <= 0:
        return
    now = datetime.now(_TZ)
    con = _db()
    with con:
        con.execute(
            "INSERT INTO costs (ts, day, api, usd) VALUES (?,?,?,?)",
            (now.isoformat(), now.strftime("%Y-%m-%d"), api, usd),
        )
    con.close()


def _rates(model: str, day: str) -> tuple[float, float]:
    if model == "claude-sonnet-5" and day <= "2026-08-31":
        return _SONNET5_INTRO
    return _RATES.get(model, _RATES["claude-sonnet-5"])


def record_anthropic(usage, model: str = "") -> None:
    """Registra o custo de uma resposta da Anthropic. Nunca lança erro."""
    try:
        model = model or config.MODEL
        day = datetime.now(_TZ).strftime("%Y-%m-%d")
        in_rate, out_rate = _rates(model, day)
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        usd = (
            inp * in_rate
            + cw * in_rate * 1.25
            + cr * in_rate * 0.1
            + out * out_rate
        ) / 1_000_000
        stu = getattr(usage, "server_tool_use", None)
        if stu is not None:
            usd += (getattr(stu, "web_search_requests", 0) or 0) * WEB_SEARCH_USD_EACH
        _add("anthropic", usd)
    except Exception:  # noqa: BLE001
        pass


def record_openai_whisper(seconds: float) -> None:
    try:
        _add("openai", (float(seconds) / 60.0) * WHISPER_USD_PER_MIN)
    except Exception:  # noqa: BLE001
        pass


def record_openai_tts(chars: int) -> None:
    try:
        _add("openai", float(chars) * TTS_USD_PER_CHAR)
    except Exception:  # noqa: BLE001
        pass


def _range(period: str) -> tuple[str, str, str]:
    hoje = datetime.now(_TZ).date()
    if period == "day":
        return hoje.isoformat(), hoje.isoformat(), "hoje"
    if period == "week":
        seg = hoje - timedelta(days=hoje.weekday())
        return seg.isoformat(), hoje.isoformat(), "esta semana"
    if period == "month":
        return hoje.replace(day=1).isoformat(), hoje.isoformat(), "este mês"
    return "0000-01-01", hoje.isoformat(), "desde o início"


def summary(period: str = "month") -> str:
    ini, fim, rotulo = _range(period)
    con = _db()
    rows = con.execute(
        "SELECT api, SUM(usd) FROM costs WHERE day>=? AND day<=? GROUP BY api",
        (ini, fim),
    ).fetchall()
    con.close()
    por = {api: v for api, v in rows}
    ant = por.get("anthropic", 0.0)
    oai = por.get("openai", 0.0)
    total = ant + oai
    if total == 0:
        return f"Sem gastos registrados em {rotulo}."
    return (
        f"Gasto estimado nas APIs — {rotulo}:\n"
        f"- Anthropic (cérebro + web): US$ {ant:.2f}\n"
        f"- OpenAI (áudio): US$ {oai:.2f}\n"
        f"- Total: US$ {total:.2f}\n"
        f"(estimativa por uso; o valor oficial fica no painel de cada API)"
    )


TOOLS = [
    {
        "name": "cost_summary",
        "description": (
            "Mostra o gasto estimado em cada API (Anthropic e OpenAI). period = 'day', "
            "'week', 'month' ou 'all' (desde o início). Padrão: month."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["day", "week", "month", "all"]}
            },
        },
    }
]

DISPATCH = {"cost_summary": lambda a: summary(a.get("period", "month"))}
