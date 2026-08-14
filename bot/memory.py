"""Memória de longo prazo do assistente (perfil do usuário).

É um arquivo de texto (Markdown) que o bot lê em toda mensagem e vai
preenchendo conforme o Állan conta fatos duráveis sobre ele. Assim o bot
'aprende' e melhora com o tempo.
"""
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

_TZ = ZoneInfo(config.TIMEZONE)


def load() -> str:
    """Devolve o conteúdo atual da memória (ou vazio)."""
    try:
        with open(config.MEMORY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def save(fact: str) -> str:
    """Acrescenta um fato durável à memória."""
    fact = fact.strip()
    if not fact:
        return "Nada para salvar."
    os.makedirs(os.path.dirname(config.MEMORY_FILE), exist_ok=True)
    data = datetime.now(_TZ).strftime("%Y-%m-%d")
    novo = not os.path.exists(config.MEMORY_FILE)
    with open(config.MEMORY_FILE, "a", encoding="utf-8") as f:
        if novo:
            f.write("# Perfil do Állan (memória do assistente)\n\n")
        f.write(f"- [{data}] {fact}\n")
    return f"Memorizado: {fact}"


def forget(trecho: str) -> str:
    """Remove todas as linhas de memória que contenham 'trecho'."""
    trecho = trecho.strip().lower()
    if not trecho:
        return "Diga o que devo esquecer."
    try:
        with open(config.MEMORY_FILE, encoding="utf-8") as f:
            linhas = f.readlines()
    except FileNotFoundError:
        return "A memória está vazia."
    mantidas = [l for l in linhas if trecho not in l.lower()]
    removidas = len(linhas) - len(mantidas)
    if removidas == 0:
        return f"Não achei nada com '{trecho}' na memória."
    with open(config.MEMORY_FILE, "w", encoding="utf-8") as f:
        f.writelines(mantidas)
    return f"Esqueci {removidas} item(ns) que mencionavam '{trecho}'."


def show() -> str:
    conteudo = load()
    return conteudo or "Ainda não sei nada sobre você. Me conte coisas e eu vou memorizando!"


# ---------------------------------------------------------------------------
# Esquemas para o Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "memory_save",
        "description": (
            "Salva na memória de longo prazo um fato DURÁVEL sobre o Állan: "
            "preferências, pessoas próximas, rotina, metas, restrições, gostos, "
            "contexto de trabalho. NÃO use para pedidos do momento nem coisas "
            "efêmeras. Escreva o fato de forma curta e clara."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "O fato a memorizar"}},
            "required": ["fact"],
        },
    },
    {
        "name": "memory_show",
        "description": "Mostra tudo o que o assistente já sabe sobre o Állan (a memória atual).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "memory_forget",
        "description": "Remove da memória os itens que contenham um trecho de texto.",
        "input_schema": {
            "type": "object",
            "properties": {"trecho": {"type": "string"}},
            "required": ["trecho"],
        },
    },
]

DISPATCH = {
    "memory_save": lambda a: save(a["fact"]),
    "memory_show": lambda a: show(),
    "memory_forget": lambda a: forget(a["trecho"]),
}
