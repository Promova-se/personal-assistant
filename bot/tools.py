"""Ferramentas do Trello expostas ao Claude, via API REST.

Cada função retorna uma string (o resultado que o Claude vê). As chaves de
API vêm de config (.trello.env). Nada aqui manda mensagem nem apaga nada
sem que o Claude peça explicitamente.
"""
from __future__ import annotations

import json

import requests

from . import config

BASE = "https://api.trello.com/1"


def _auth() -> dict:
    return {"key": config.TRELLO_KEY, "token": config.TRELLO_TOKEN}


def _get(path: str, **params) -> object:
    r = requests.get(f"{BASE}{path}", params={**_auth(), **params}, timeout=20)
    r.raise_for_status()
    return r.json()


def _post(path: str, **params) -> object:
    r = requests.post(f"{BASE}{path}", params={**_auth(), **params}, timeout=20)
    r.raise_for_status()
    return r.json()


def _put(path: str, **params) -> object:
    r = requests.put(f"{BASE}{path}", params={**_auth(), **params}, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Implementações
# ---------------------------------------------------------------------------

def list_boards() -> str:
    boards = _get("/members/me/boards", filter="open", fields="name")
    linhas = [f"- {b['name']} (id: {b['id']})" for b in boards]
    return "Quadros abertos:\n" + "\n".join(linhas)


def list_cards(board_id: str = "", only_due: bool = False) -> str:
    """Lista cards. Se board_id vazio, varre todos os quadros abertos."""
    if board_id:
        boards = [{"id": board_id, "name": ""}]
    else:
        boards = _get("/members/me/boards", filter="open", fields="name")

    out: list[str] = []
    for b in boards:
        lists = _get(
            f"/boards/{b['id']}/lists",
            fields="name",
            cards="open",
            card_fields="name,due,dueComplete",
        )
        nome_quadro = b.get("name") or b["id"]
        bloco: list[str] = []
        for lst in lists:
            cards = lst.get("cards", [])
            if only_due:
                cards = [c for c in cards if c.get("due") and not c.get("dueComplete")]
            if not cards:
                continue
            bloco.append(f"  Lista: {lst['name']}")
            for c in cards:
                due = c.get("due")
                due_txt = f" | vence: {due}" if due else ""
                bloco.append(f"    • {c['name']} (id: {c['id']}){due_txt}")
        if bloco:
            out.append(f"Quadro: {nome_quadro}")
            out.extend(bloco)
    return "\n".join(out) if out else "Nenhum card encontrado com esse filtro."


def list_lists(board_id: str) -> str:
    lists = _get(f"/boards/{board_id}/lists", fields="name")
    linhas = [f"- {l['name']} (id: {l['id']})" for l in lists]
    return "Listas do quadro:\n" + "\n".join(linhas)


def create_card(list_id: str, name: str, desc: str = "", due: str = "") -> str:
    params = {"idList": list_id, "name": name}
    if desc:
        params["desc"] = desc
    if due:
        params["due"] = due
    card = _post("/cards", **params)
    return f"Card criado: {card['name']} (id: {card['id']}) — {card.get('shortUrl', '')}"


def update_card(
    card_id: str,
    name: str = "",
    due: str = "",
    due_complete: bool | None = None,
    list_id: str = "",
) -> str:
    params: dict = {}
    if name:
        params["name"] = name
    if due:
        params["due"] = due
    if due_complete is not None:
        params["dueComplete"] = "true" if due_complete else "false"
    if list_id:
        params["idList"] = list_id
    if not params:
        return "Nada para atualizar (nenhum campo informado)."
    card = _put(f"/cards/{card_id}", **params)
    return f"Card atualizado: {card['name']} (id: {card['id']})"


# ---------------------------------------------------------------------------
# Dispatch + esquemas para o Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "trello_list_boards",
        "description": "Lista todos os quadros (boards) abertos do Trello do usuário, com seus IDs.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "trello_list_cards",
        "description": (
            "Lista cards do Trello agrupados por quadro e lista. Use para ver "
            "tarefas. Se board_id for vazio, varre todos os quadros. Use "
            "only_due=true para trazer só os cards com prazo (due) não concluído."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "board_id": {"type": "string", "description": "ID do quadro; vazio = todos"},
                "only_due": {"type": "boolean", "description": "Só cards com prazo pendente"},
            },
        },
    },
    {
        "name": "trello_list_lists",
        "description": "Lista as listas (colunas) de um quadro, com seus IDs. Necessário para criar um card na lista certa.",
        "input_schema": {
            "type": "object",
            "properties": {"board_id": {"type": "string"}},
            "required": ["board_id"],
        },
    },
    {
        "name": "trello_create_card",
        "description": "Cria um card novo numa lista. Precisa do list_id (use trello_list_lists antes se não souber).",
        "input_schema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string"},
                "name": {"type": "string", "description": "Título do card"},
                "desc": {"type": "string", "description": "Descrição (opcional)"},
                "due": {"type": "string", "description": "Prazo em ISO 8601 (opcional), ex: 2026-08-15T02:59:00.000Z"},
            },
            "required": ["list_id", "name"],
        },
    },
    {
        "name": "trello_update_card",
        "description": (
            "Atualiza um card existente: renomear (name), mudar prazo (due), "
            "marcar concluído (due_complete=true), ou mover para outra lista (list_id)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "name": {"type": "string"},
                "due": {"type": "string", "description": "Novo prazo ISO 8601"},
                "due_complete": {"type": "boolean"},
                "list_id": {"type": "string", "description": "Mover para esta lista"},
            },
            "required": ["card_id"],
        },
    },
]

_DISPATCH = {
    "trello_list_boards": lambda a: list_boards(),
    "trello_list_cards": lambda a: list_cards(a.get("board_id", ""), a.get("only_due", False)),
    "trello_list_lists": lambda a: list_lists(a["board_id"]),
    "trello_create_card": lambda a: create_card(
        a["list_id"], a["name"], a.get("desc", ""), a.get("due", "")
    ),
    "trello_update_card": lambda a: update_card(
        a["card_id"],
        a.get("name", ""),
        a.get("due", ""),
        a.get("due_complete"),
        a.get("list_id", ""),
    ),
}

# Google Agenda (só entra se a credencial existir)
if config.GCAL_ENABLED:
    from . import gcal

    TOOLS = TOOLS + gcal.TOOLS
    _DISPATCH.update(gcal.DISPATCH)


def run_tool(name: str, args: dict) -> str:
    """Executa a ferramenta e devolve o resultado como texto."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"Ferramenta desconhecida: {name}"
    try:
        return str(fn(args))
    except requests.HTTPError as e:
        return f"Erro na API do Trello ({e.response.status_code}): {e.response.text[:300]}"
    except Exception as e:  # noqa: BLE001
        return f"Erro ao executar {name}: {e!r}\nArgs: {json.dumps(args, ensure_ascii=False)}"
