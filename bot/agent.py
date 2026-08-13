"""O 'cérebro': conversa com o Claude e executa as ferramentas do Trello.

Mantém um histórico curto por chat (em memória). Cada mensagem do usuário
roda um laço: Claude pensa -> pode pedir ferramentas -> devolvemos o
resultado -> Claude responde em texto.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from . import config, tools

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# Histórico por chat_id -> lista de mensagens (formato da API)
_history: dict[int, list[dict]] = {}

MAX_TURNS = 24  # mantém as últimas ~12 trocas para não crescer sem limite
MAX_TOOL_LOOPS = 8


def _system_prompt() -> str:
    agora = datetime.now(ZoneInfo(config.TIMEZONE))
    return (
        "Você é o assistente pessoal do Állan, falando por Telegram em português "
        "do Brasil. Seja direto, prático e cordial — respostas curtas, sem enrolação.\n"
        f"Agora: {agora.strftime('%A, %d/%m/%Y %H:%M')} ({config.TIMEZONE}).\n\n"
        "Você tem ferramentas para ler e editar o Trello do Állan (quadros, listas, "
        "cards, prazos). Use-as quando ele pedir para ver, criar, mover ou concluir "
        "tarefas. Antes de criar um card, descubra o list_id certo com as ferramentas "
        "de listagem se necessário.\n\n"
        "Você também tem acesso à Google Agenda dele (gcal_*): ver eventos do dia/semana "
        "e criar/remover compromissos. Ao criar eventos, use ISO 8601 com o fuso "
        f"({config.TIMEZONE}, ou seja -03:00). Ao mostrar a agenda, seja objetivo.\n\n"
        "Regras de segurança: você pode criar e atualizar cards livremente, mas NUNCA "
        "invente dados. Ao concluir ou mover algo importante, confirme brevemente o que "
        "fez. Datas de prazo no Trello costumam ser gravadas às 02:59Z (fim do dia no "
        "horário de Brasília) — respeite esse padrão ao criar prazos 'para hoje/amanhã'."
    )


def reset(chat_id: int) -> None:
    _history.pop(chat_id, None)


def handle_message(chat_id: int, text: str) -> str:
    """Processa uma mensagem do usuário e devolve a resposta em texto."""
    msgs = _history.setdefault(chat_id, [])
    msgs.append({"role": "user", "content": text})

    for _ in range(MAX_TOOL_LOOPS):
        resp = _client.messages.create(
            model=config.MODEL,
            max_tokens=4096,
            system=_system_prompt(),
            tools=tools.TOOLS,
            output_config={"effort": "low"},
            messages=msgs,
        )

        # Guarda a resposta do assistente (com blocos de tool_use) no histórico
        msgs.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "tool_use":
            resultados = []
            for bloco in resp.content:
                if bloco.type == "tool_use":
                    saida = tools.run_tool(bloco.name, bloco.input or {})
                    resultados.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": bloco.id,
                            "content": saida,
                        }
                    )
            msgs.append({"role": "user", "content": resultados})
            continue  # deixa o Claude ler os resultados e continuar

        # Sem mais ferramentas: junta o texto final
        texto = "".join(b.text for b in resp.content if b.type == "text").strip()
        _trim(chat_id)
        return texto or "(sem resposta)"

    _trim(chat_id)
    return "Precisei de muitos passos e parei por segurança. Pode reformular?"


def _trim(chat_id: int) -> None:
    msgs = _history.get(chat_id, [])
    if len(msgs) > MAX_TURNS:
        # Descarta o começo, mas garante que a lista não comece com tool_result
        corte = len(msgs) - MAX_TURNS
        novo = msgs[corte:]
        while novo and novo[0].get("role") != "user":
            novo = novo[1:]
        # remove eventual user cujo conteúdo seja só tool_result órfão
        while novo and _is_tool_result_only(novo[0]):
            novo = novo[1:]
        _history[chat_id] = novo


def _is_tool_result_only(msg: dict) -> bool:
    content = msg.get("content")
    if isinstance(content, list):
        return all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False
