"""Vigia o painel de monitoramento de sites (projeto /opt/status, no mesmo servidor).

O painel checa as lojas do Állan a cada poucos minutos e grava tudo num SQLite.
Aqui só LEMOS esse banco (somente leitura) e decidimos quando vale avisar no
Telegram. O alerta embutido do painel dispara na PRIMEIRA checagem que falha
(dezenas de falsos alarmes por semana, a maioria um único timeout) e também
avisa "lento", então não serve direto.

Regras (definidas pelo Állan: só importa saber QUE UM SITE DELE CAIU):
- só avisa dos sites da categoria "mine" (config.SITEWATCH_CATEGORIES);
- "caiu" = as últimas 2 checagens falharam (~10 min). Um aviso por queda (vários
  sites caindo juntos viram UMA mensagem). Sem aviso de "voltou", sem lembrete,
  sem lento/SSL/tags — ele não quer;
- única exceção: se o próprio painel parar de checar, avisa uma vez (senão o
  silêncio pareceria "tudo certo").
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo(config.TIMEZONE)

CONFIRMAR = 2           # checagens seguidas para confirmar queda / volta
PAINEL_PAROU_MIN = 20   # sem nenhuma checagem nova há mais que isso = painel parou
AGRUPAR_ATE_MIN = 10    # espera até isso por outros sites do mesmo evento, p/ 1 mensagem só
HISTORICO = 300         # checagens lidas por site (~25h, a cada 5 min)
AVISOS_GUARDADOS = 6    # últimos avisos lembrados (contexto p/ o bot)

_ESTADO = os.path.join(os.path.dirname(config.DIET_DB), "sitewatch.json")


# ---------------------------------------------------------------------------
# Leitura do banco do painel (Prisma/SQLite; datas em milissegundos)
# ---------------------------------------------------------------------------

def _conectar() -> sqlite3.Connection:
    uri = Path(config.STATUS_DB).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _sites(con: sqlite3.Connection, categorias: list[str]) -> list[sqlite3.Row]:
    marcas = ",".join("?" * len(categorias))  # só placeholders; valores vão como parâmetro
    return con.execute(
        f"SELECT id, name, url, category FROM Site WHERE active=1 "
        f"AND category IN ({marcas}) ORDER BY name",
        categorias,
    ).fetchall()


def _checagens(con: sqlite3.Connection, site_id: str, ate_ms: int, n: int = HISTORICO):
    """Últimas checagens do site (mais recente primeiro), até o instante dado."""
    return con.execute(
        'SELECT online, statusCode, error, latencyMs, checkedAt FROM "Check" '
        "WHERE siteId=? AND checkedAt<=? ORDER BY checkedAt DESC LIMIT ?",
        (site_id, ate_ms, n),
    ).fetchall()


def _agora_ms() -> int:
    return int(datetime.now(_TZ).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------

def _motivo(c) -> str:
    if c["statusCode"]:
        return f"HTTP {c['statusCode']}"
    return (c["error"] or "sem resposta").rstrip(".")


def _hora(ms: int, ref_ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, _TZ)
    ref = datetime.fromtimestamp(ref_ms / 1000, _TZ)
    return dt.strftime("%H:%M") if dt.date() == ref.date() else dt.strftime("%d/%m %H:%M")


def _dur(ms: int) -> str:
    m = max(0, int(ms // 60_000))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d} dia{'s' if d > 1 else ''}" + (f" e {h}h" if h else "")


def _bloco(um: str, varios: str, linhas: list[str]) -> str:
    cab = um if len(linhas) == 1 else varios.format(n=len(linhas))
    return cab + "\n" + "\n".join(linhas)


def _inicio_falha(rows) -> int:
    """Quando começou a sequência de falhas que termina na checagem mais recente."""
    inicio = rows[0]["checkedAt"]
    for c in rows:
        if c["online"]:
            break
        inicio = c["checkedAt"]
    return inicio


# ---------------------------------------------------------------------------
# Estado persistido (pra não repetir aviso depois de reiniciar o bot)
# ---------------------------------------------------------------------------

def carregar_estado() -> dict:
    try:
        with open(_ESTADO, encoding="utf-8") as f:
            e = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        e = {}
    e.setdefault("fora", {})            # site_id -> {nome, url, desde, motivo}
    e.setdefault("painel_parado", False)
    e.setdefault("avisos", [])          # [{em, texto}] — o que já foi avisado
    return e


def salvar_estado(estado: dict) -> None:
    if estado == carregar_estado():
        return
    os.makedirs(os.path.dirname(_ESTADO), exist_ok=True)
    tmp = _ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False)
    os.replace(tmp, _ESTADO)


# ---------------------------------------------------------------------------
# Decisão: o que avisar agora
# ---------------------------------------------------------------------------

def poll(agora_ms: int | None = None) -> tuple[list[str], dict]:
    """Lê o painel e devolve (mensagens a enviar, novo estado). NÃO envia nem grava:
    quem chama grava (salvar_estado) só depois de enviar com sucesso — se o envio
    falhar, o aviso é tentado de novo no ciclo seguinte em vez de se perder.
    `agora_ms` existe pra testar 'voltando no tempo' sobre dados reais."""
    agora = agora_ms or _agora_ms()
    estado = carregar_estado()
    msgs: list[str] = []

    con = _conectar()
    try:
        sites = _sites(con, config.SITEWATCH_CATEGORIES)

        # O painel continua checando? Se parou, não dá pra avaliar nada.
        ultimo = con.execute(
            'SELECT MAX(c.checkedAt) FROM "Check" c JOIN Site s ON s.id=c.siteId '
            "WHERE s.active=1 AND c.checkedAt<=?",
            (agora,),
        ).fetchone()[0]
        parou = ultimo is None or agora - ultimo > PAINEL_PAROU_MIN * 60_000
        if parou:
            if not estado["painel_parado"]:
                quando = (
                    f"última checagem às {_hora(ultimo, agora)}" if ultimo else "nenhuma checagem registrada"
                )
                msgs.append(
                    f"⚠️ O painel de monitoramento parou de checar os sites ({quando}). "
                    "Enquanto isso eu não consigo avisar de quedas."
                )
            return _fechar(estado, estado["fora"], True, msgs, agora)
        # (o painel voltar a checar é registrado em silêncio: o estado vira "não parado")

        ids = {s["id"] for s in sites}
        # cópia, sem sites que foram desativados/removidos/mudaram de categoria
        fora = {sid: dict(v) for sid, v in estado["fora"].items() if sid in ids}
        novos = []          # (site, checagens) que acabaram de cair
        quase_caindo = 0    # 1ª falha ainda sem confirmar

        for s in sites:
            rows = _checagens(con, s["id"], agora)
            if not rows:
                continue
            ultimas = rows[:CONFIRMAR]
            confirmavel = len(ultimas) == CONFIRMAR
            caiu = confirmavel and not any(c["online"] for c in ultimas)
            voltou = confirmavel and all(c["online"] for c in ultimas)

            if caiu and s["id"] not in fora:
                novos.append((s, rows))
            elif voltou and s["id"] in fora:
                # Voltou: sem aviso (o Állan não quer), mas some do "fora" — senão a
                # próxima queda deste site não geraria aviso.
                del fora[s["id"]]
            elif not caiu and not rows[0]["online"] and s["id"] not in fora:
                quase_caindo += 1
    finally:
        con.close()

    # As checagens do painel são em sequência: numa queda de vários sites, o último
    # confirma segundos depois dos outros — e, com ciclos de 2 min, viraria uma 2ª
    # mensagem. Se tem site "quase confirmando" e o evento ainda é recente, espera
    # o próximo ciclo pra mandar tudo junto (limitado, pra nunca atrasar demais).
    janela = AGRUPAR_ATE_MIN * 60_000
    if novos and quase_caindo and agora - min(_inicio_falha(r) for _s, r in novos) < janela:
        novos = []

    avisar = []
    for s, rows in novos:
        fora[s["id"]] = {
            "nome": s["name"], "url": s["url"], "desde": _inicio_falha(rows),
            "motivo": _motivo(rows[0]),
        }
        avisar.append(fora[s["id"]])

    if avisar:
        linhas = [
            f"• {f['nome']} — {f['motivo']} "
            f"(desde {_hora(f['desde'], agora)}, há {_dur(agora - f['desde'])})"
            for f in avisar
        ]
        msgs.append(_bloco("🔴 Site fora do ar", "🔴 {n} sites fora do ar", linhas))
    return _fechar(estado, fora, False, msgs, agora)


def _fechar(estado: dict, fora: dict, painel_parado: bool, msgs: list[str], agora: int):
    """Monta o novo estado, guardando os avisos das últimas 24h (contexto pro bot)."""
    avisos = [a for a in estado["avisos"] if agora - a["em"] < 24 * 3_600_000]
    avisos += [{"em": agora, "texto": m} for m in msgs]
    novo = {**estado, "fora": fora, "painel_parado": painel_parado, "avisos": avisos[-AVISOS_GUARDADOS:]}
    return msgs, novo


def avisos_recentes() -> str:
    """Avisos automáticos das últimas 24h, em texto, pra ir no prompt do bot —
    assim, se o Állan responder a um aviso ('o que houve?'), o bot sabe do que se trata."""
    agora = _agora_ms()
    linhas = []
    for a in carregar_estado()["avisos"]:
        if agora - a["em"] < 24 * 3_600_000:
            linhas.append(f"- [{_hora(a['em'], agora)}] " + a["texto"].replace("\n", " / "))
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Consulta: "como estão meus sites?"
# ---------------------------------------------------------------------------

def status_text(incluir_concorrentes: bool = False) -> str:
    cats = list(config.SITEWATCH_CATEGORIES)
    if incluir_concorrentes:
        cats = sorted(set(cats) | {"mine", "competitor"})
    agora = _agora_ms()

    con = _conectar()
    try:
        sites = _sites(con, cats)
        if not sites:
            return "Nenhum site ativo no painel de monitoramento."
        ultimo = con.execute(
            'SELECT MAX(c.checkedAt) FROM "Check" c JOIN Site s ON s.id=c.siteId WHERE s.active=1'
        ).fetchone()[0]
        itens = []
        for s in sites:
            rows = _checagens(con, s["id"], agora)
            etiqueta = " (concorrente)" if s["category"] == "competitor" else ""
            if not rows:
                itens.append((3, f"⚪ {s['name']}{etiqueta} — sem checagens ainda"))
                continue
            dia = [c for c in rows if c["checkedAt"] >= agora - 24 * 3_600_000]
            pct = 100.0 * sum(1 for c in dia if c["online"]) / len(dia) if dia else 0.0
            ult = rows[0]
            if ult["online"]:
                ordem = 2
                lat = f" ({ult['latencyMs']} ms)" if ult["latencyMs"] else ""
                est = f"✅ no ar{lat}"
            elif len(rows) > 1 and rows[1]["online"]:
                ordem = 1
                est = f"🟡 falhou na última checagem ({_motivo(ult)}) — pode ser instabilidade passageira"
            else:
                ordem = 0
                desde = _inicio_falha(rows)
                est = f"🔴 FORA — {_motivo(ult)} desde {_hora(desde, agora)} (há {_dur(agora - desde)})"
            itens.append((ordem, f"{s['name']}{etiqueta} — {est} · últimas 24h: {pct:.1f}% no ar"))
    finally:
        con.close()

    itens.sort(key=lambda x: x[0])  # problemas primeiro
    cab = f"Sites monitorados — última checagem às {_hora(ultimo, agora)}:" if ultimo else "Sites monitorados:"
    if ultimo and agora - ultimo > PAINEL_PAROU_MIN * 60_000:
        cab += "\n⚠️ O painel está há muito tempo sem checar — os dados abaixo podem estar velhos."
    return cab + "\n" + "\n".join(t for _o, t in itens)


# ---------------------------------------------------------------------------
# Esquema para o Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "sites_status",
        "description": (
            "Mostra como estão as lojas/sites do Állan no painel de monitoramento: quais estão "
            "no ar ou fora, desde quando, e a disponibilidade das últimas 24h. Use quando ele "
            "perguntar dos sites/lojas ou de uma queda. include_competitors=true só se ele "
            "pedir também os concorrentes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"include_competitors": {"type": "boolean"}},
        },
    }
]

DISPATCH = {"sites_status": lambda a: status_text(bool(a.get("include_competitors", False)))}
