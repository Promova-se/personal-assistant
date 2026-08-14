"""Base de conhecimento: pasta de arquivos .md que o bot consulta e amplia.

Cada arquivo é um 'assunto' (ex: cupido, vendas, investimentos). O bot lista/lê
quando o tema é relevante, e o Állan pode ensinar coisas novas a qualquer momento
(o bot salva com knowledge_save). É assim que o assistente fica mais inteligente
com o tempo.
"""
from __future__ import annotations

import os
import re

from . import config


def _dir() -> str:
    os.makedirs(config.KNOWLEDGE_DIR, exist_ok=True)
    return config.KNOWLEDGE_DIR


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-")
    return (base or "nota") + ".md"


def list_topics() -> str:
    d = _dir()
    arqs = sorted(f for f in os.listdir(d) if f.endswith(".md"))
    if not arqs:
        return "A base de conhecimento está vazia. Você pode me ensinar assuntos novos."
    linhas = ["Assuntos na base de conhecimento:"]
    for f in arqs:
        nome = f[:-3]
        try:
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                primeira = fh.readline().strip().lstrip("# ").strip()
        except OSError:
            primeira = ""
        linhas.append(f"- {nome}" + (f" — {primeira}" if primeira else ""))
    return "\n".join(linhas)


def read_topic(name: str) -> str:
    caminho = os.path.join(_dir(), _slug(name))
    try:
        with open(caminho, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return f"Não achei o assunto '{name}'. Veja os disponíveis com knowledge_list."


def save_topic(name: str, content: str, append: bool = True) -> str:
    caminho = os.path.join(_dir(), _slug(name))
    existe = os.path.exists(caminho)
    modo = "a" if (append and existe) else "w"
    with open(caminho, modo, encoding="utf-8") as f:
        if modo == "w":
            f.write(f"# {name.strip()}\n\n")
        f.write(content.strip() + "\n")
    acao = "ampliado" if (append and existe) else "criado/atualizado"
    return f"Conhecimento '{name}' {acao}."


# ---------------------------------------------------------------------------
# Esquemas para o Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "knowledge_list",
        "description": "Lista os assuntos disponíveis na base de conhecimento.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "knowledge_read",
        "description": (
            "Lê o conteúdo de um assunto da base de conhecimento (ex: 'cupido'). "
            "Consulte antes de agir quando o tema tiver material salvo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "knowledge_save",
        "description": (
            "Salva/amplia um assunto na base de conhecimento. Use quando o Állan pedir "
            "'aprende isso', mandar material de referência, ou ensinar algo que ele quer "
            "que você domine dali em diante. append=false sobrescreve; true acrescenta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nome do assunto (ex: cupido, vendas)"},
                "content": {"type": "string"},
                "append": {"type": "boolean"},
            },
            "required": ["name", "content"],
        },
    },
]

DISPATCH = {
    "knowledge_list": lambda a: list_topics(),
    "knowledge_read": lambda a: read_topic(a["name"]),
    "knowledge_save": lambda a: save_topic(a["name"], a["content"], a.get("append", True)),
}
