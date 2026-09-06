"""Arquivos/fotos que o usuário já enviou, guardados para 'revisitar' depois.

Guarda os bytes originais em disco + um índice em SQLite (id, data, legenda,
tipo). O bot pode reabrir (files_view) um arquivo antigo e recolocá-lo no
contexto como se tivesse acabado de receber — inclusive fotos, que voltam
como imagem de verdade, não só descrição em texto.
"""
from __future__ import annotations

import base64
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo(config.TIMEZONE)
_DIR = os.path.join(os.path.dirname(config.DIET_DB), "uploads")
_DB = os.path.join(os.path.dirname(config.DIET_DB), "files.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(_DIR, exist_ok=True)
    con = sqlite3.connect(_DB)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            kind TEXT NOT NULL,        -- 'photo' | 'document'
            path TEXT NOT NULL,
            media_type TEXT NOT NULL,
            caption TEXT,
            created_ts TEXT NOT NULL
        )
        """
    )
    return con


def save(
    chat_id: int,
    data: bytes,
    media_type: str,
    caption: str = "",
    kind: str = "photo",
    ext: str = "jpg",
) -> int:
    """Salva o arquivo em disco e registra no índice. Devolve o id."""
    now = datetime.now(_TZ)
    con = _conn()
    with con:
        cur = con.execute(
            "INSERT INTO files (chat_id, kind, path, media_type, caption, created_ts) "
            "VALUES (?,?,?,?,?,?)",
            (chat_id, kind, "", media_type, caption, now.isoformat()),
        )
        fid = cur.lastrowid
        path = os.path.join(_DIR, f"{fid}.{ext}")
        con.execute("UPDATE files SET path=? WHERE id=?", (path, fid))
    con.close()
    with open(path, "wb") as f:
        f.write(data)
    return fid


def list_files(chat_id: int, limit: int = 20) -> str:
    con = _conn()
    rows = con.execute(
        "SELECT id, kind, caption, created_ts FROM files WHERE chat_id=? "
        "ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    con.close()
    if not rows:
        return "Nenhum arquivo salvo ainda."
    linhas = ["Arquivos recentes:"]
    for fid, kind, caption, ts in rows:
        try:
            dt = datetime.fromisoformat(ts).strftime("%d/%m %H:%M")
        except ValueError:
            dt = ts
        legenda = f' — "{caption}"' if caption else ""
        linhas.append(f"  • #{fid} {dt} [{kind}]{legenda}")
    return "\n".join(linhas)


def view_file(file_id: int):
    """Reabre um arquivo salvo. Fotos voltam como imagem de verdade (bloco
    'image'); PDFs voltam como documento de verdade (bloco 'document', lido
    nativamente pelo Claude); outros documentos voltam como texto. Devolve
    uma lista de blocos de conteúdo (formato aceito pela API em tool_result)."""
    con = _conn()
    row = con.execute(
        "SELECT kind, path, media_type, caption FROM files WHERE id=?", (file_id,)
    ).fetchone()
    con.close()
    if row is None:
        return f"Não achei o arquivo #{file_id}."
    kind, path, media_type, caption = row
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return f"O arquivo #{file_id} não está mais disponível no servidor."
    nota = f"(arquivo #{file_id}" + (f', legenda original: "{caption}"' if caption else "") + ")"
    if kind == "photo":
        b64 = base64.standard_b64encode(data).decode()
        return [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": nota},
        ]
    if kind == "pdf":
        b64 = base64.standard_b64encode(data).decode()
        return [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": nota},
        ]
    texto = data.decode("utf-8", errors="replace")[:100_000]
    return [{"type": "text", "text": f"{nota}\n\n{texto}"}]


def cleanup_old(days: int = 90) -> int:
    """Remove do disco e do índice os arquivos mais antigos que 'days' dias.
    Devolve quantos foram removidos."""
    limite = (datetime.now(_TZ) - timedelta(days=days)).isoformat()
    con = _conn()
    rows = con.execute(
        "SELECT id, path FROM files WHERE created_ts < ?", (limite,)
    ).fetchall()
    for _fid, path in rows:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    with con:
        con.execute("DELETE FROM files WHERE created_ts < ?", (limite,))
    con.close()
    return len(rows)


# ---------------------------------------------------------------------------
# Esquemas para o Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "files_list",
        "description": (
            "Lista os arquivos/fotos que o Állan enviou recentemente (mais recentes "
            "primeiro), com id, data, tipo e legenda. Use para achar um arquivo que ele "
            "quer que você reveja (ex: 'aquela foto da fatura', 'o documento de ontem')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "padrão 20"}},
        },
    },
    {
        "name": "files_view",
        "description": (
            "Reabre um arquivo/foto enviado anteriormente pelo id (veja o #id em "
            "files_list) e traz de volta para você analisar, como se tivesse acabado de "
            "receber. Fotos voltam como imagem de verdade — você pode olhar de novo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"file_id": {"type": "integer"}},
            "required": ["file_id"],
        },
    },
]

DISPATCH = {
    "files_list": lambda a: list_files(a["_chat_id"], a.get("limit", 20)),
    "files_view": lambda a: view_file(a["file_id"]),
}
