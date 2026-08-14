"""O 'cérebro': conversa com o Claude e executa as ferramentas do Trello.

Mantém um histórico curto por chat (em memória). Cada mensagem do usuário
roda um laço: Claude pensa -> pode pedir ferramentas -> devolvemos o
resultado -> Claude responde em texto.
"""
from __future__ import annotations

import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from . import config, memory, tools

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# Histórico por chat_id -> lista de mensagens (formato da API)
_history: dict[int, list[dict]] = {}

MAX_TURNS = 24  # mantém as últimas ~12 trocas para não crescer sem limite
MAX_TOOL_LOOPS = 8


def _system_prompt() -> str:
    agora = datetime.now(ZoneInfo(config.TIMEZONE))
    base = (
        "Você é o assistente pessoal do Állan, falando por Telegram em português "
        "do Brasil. Seja direto, prático e cordial — respostas curtas, sem enrolação.\n"
        f"Agora: {agora.strftime('%A, %d/%m/%Y %H:%M')} ({config.TIMEZONE}).\n\n"
        "Você tem ferramentas para ler e editar o Trello do Állan (quadros, listas, "
        "cards, prazos). Use-as quando ele pedir para ver, criar, mover ou concluir "
        "tarefas. Antes de criar um card, descubra o list_id certo com as ferramentas "
        "de listagem se necessário.\n\n"
        "Você também tem acesso à Google Agenda dele (gcal_*): ver eventos do dia/semana "
        "e criar/remover compromissos. Ao criar eventos, use ISO 8601 com o fuso "
        f"({config.TIMEZONE}, ou seja -03:00). Ao mostrar a agenda, seja objetivo. "
        "Pode haver várias agendas — use gcal_list_calendars para ver as disponíveis e "
        "passe o parâmetro 'calendar' (apelido ou ID) quando ele indicar uma específica. "
        "Se ele citar uma agenda que não está na lista, peça o ID dela.\n\n"
        "Você também ajuda na DIETA dele (diet_*). Quando ele mandar uma FOTO DE COMIDA "
        "(prato, lanche, doce, bebida), estime as calorias (kcal) e, se der, os macros "
        "(proteína/carbo/gordura em g) e registre com diet_log_meal, confirmando de forma "
        "curta. Quando ele pedir 'quanto comi hoje/essa semana/esse mês', use diet_summary. "
        "Seja realista nas estimativas e diga que são aproximadas.\n\n"
        "SOBRE FOTOS — decida pelo conteúdo:\n"
        "- Foto de COMIDA → registrar refeição (diet_log_meal).\n"
        "- Foto de EVENTO (convite, panfleto, print de horário, cartaz) → criar evento na "
        "Google Agenda (gcal_create_event); se faltar data ou hora, pergunte antes de "
        "criar; assuma o ano atual se a imagem não informar.\n"
        "- Se estiver ambíguo, pergunte o que ele quer.\n\n"
        "FINANCEIRO (fin_*) e EXERCÍCIOS (ex_*): mesma mecânica da dieta. Quando ele "
        "disser o que gastou ('gastei 50 no mercado'), registre com fin_log e, quando "
        "pedir, use fin_summary — dê feedback útil (categorias que pesaram, comparação). "
        "Quando contar um treino ('corri 5km em 30min'), registre com ex_log; resumos com "
        "ex_summary. Seja um bom acompanhante: elogie consistência, aponte tendências.\n\n"
        "MEMÓRIA: quando o Állan contar algo DURÁVEL sobre ele (preferências, pessoas "
        "próximas, rotina, metas, restrições, contexto de trabalho), salve com memory_save "
        "sem alarde. Use a memória abaixo para personalizar suas respostas. Se ele pedir "
        "'o que você sabe sobre mim', use memory_show; para apagar, memory_forget.\n\n"
        "INTERNET: você pode PESQUISAR na web (web_search) e ABRIR páginas/links (web_fetch) "
        "quando precisar de informação atual (notícias, preços, cotações, horários, clima, "
        "pesquisa) ou pra resumir um link que ele mandar. Pesquise só quando agregar valor e "
        "cite a fonte de forma curta. Não use pra coisas que você já sabe.\n\n"
        "BASE DE CONHECIMENTO (knowledge_*): você tem assuntos que pode dominar. Quando o "
        "tema tiver material salvo (ex: conselhos amorosos → 'cupido'), consulte "
        "knowledge_read antes de responder. O Állan pode te ENSINAR coisas novas a qualquer "
        "momento: se ele disser 'aprende isso', mandar material de referência, ou pedir que "
        "você domine um assunto, salve com knowledge_save. Use knowledge_list quando fizer "
        "sentido descobrir o que já sabe.\n"
        "CUPIDO / CONSELHEIRO AMOROSO: quando ele pedir ajuda pra conquistar/conversar com "
        "alguém, leia o assunto 'cupido' (knowledge_read) e atue como um bom conselheiro — "
        "prático, respeitoso e autêntico: sugira aberturas de conversa, mensagens, leitura "
        "de sinais e próximos passos. Nunca sugira manipulação, mentira ou desrespeito a "
        "limites. Pergunte o contexto que faltar.\n\n"
        "Regras de segurança: você pode criar e atualizar cards livremente, mas NUNCA "
        "invente dados. Ao concluir ou mover algo importante, confirme brevemente o que "
        "fez. Datas de prazo no Trello costumam ser gravadas às 02:59Z (fim do dia no "
        "horário de Brasília) — respeite esse padrão ao criar prazos 'para hoje/amanhã'."
    )
    perfil = memory.load()
    if perfil:
        base += "\n\n## O que você já sabe sobre o Állan (memória)\n" + perfil
    return base


def reset(chat_id: int) -> None:
    _history.pop(chat_id, None)


def handle_message(
    chat_id: int,
    text: str,
    image_bytes: bytes | None = None,
    media_type: str = "image/jpeg",
) -> str:
    """Processa uma mensagem do usuário (texto e/ou imagem) e devolve a resposta."""
    msgs = _history.setdefault(chat_id, [])

    if image_bytes:
        b64 = base64.standard_b64encode(image_bytes).decode()
        legenda = text or (
            "Extraia o evento desta imagem e adicione à minha Google Agenda. "
            "Se faltar data ou hora, me pergunte."
        )
        msgs.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": legenda},
                ],
            }
        )
    else:
        msgs.append({"role": "user", "content": text})

    resposta = _run(msgs)
    _trim(chat_id)
    return resposta


def _run(msgs: list[dict]) -> str:
    """Executa o laço de ferramentas sobre a lista de mensagens dada."""
    for _ in range(MAX_TOOL_LOOPS):
        resp = _client.messages.create(
            model=config.MODEL,
            max_tokens=4096,
            system=_system_prompt(),
            tools=tools.TOOLS,
            output_config={"effort": "low"},
            messages=msgs,
        )
        msgs.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "tool_use":
            resultados = []
            for bloco in resp.content:
                if bloco.type == "tool_use":
                    saida = tools.run_tool(bloco.name, bloco.input or {})
                    resultados.append(
                        {"type": "tool_result", "tool_use_id": bloco.id, "content": saida}
                    )
            msgs.append({"role": "user", "content": resultados})
            continue

        # Ferramentas do servidor (busca web) podem pausar; reenvia pra continuar
        if resp.stop_reason == "pause_turn":
            continue

        return "".join(b.text for b in resp.content if b.type == "text").strip() or "(sem resposta)"

    return "Precisei de muitos passos e parei por segurança. Pode reformular?"


def handle_document(
    chat_id: int, filename: str, content: str, instruction: str = ""
) -> str:
    """Lê um documento anexado (ex: export de conversa), resume e aprende — sem
    guardar o texto gigante no histórico."""
    msgs = _history.setdefault(chat_id, [])
    MAX_CHARS = 400_000
    truncado = len(content) > MAX_CHARS
    if truncado:
        content = content[:MAX_CHARS]
    pedido = instruction.strip() or (
        "Resuma este conteúdo e liste os PRINCIPAIS PONTOS DE ATENÇÃO (pendências, "
        "decisões, prazos, cobranças, riscos, quem espera algo de mim). Se houver algo "
        "durável sobre mim, memorize com memory_save."
    )
    if truncado:
        pedido += "\n(Obs: conteúdo cortado por tamanho; resuma o que veio.)"
    prompt = f"Documento anexado: {filename}\n{pedido}\n\n--- CONTEÚDO ---\n{content}"

    # Roda numa cópia do histórico para NÃO persistir o texto gigante
    work = msgs + [{"role": "user", "content": prompt}]
    resposta = _run(work)

    # No histórico real, guarda só um registro compacto + a resposta
    msgs.append(
        {"role": "user", "content": f"[Enviei o documento '{filename}' e pedi um resumo/análise.]"}
    )
    msgs.append({"role": "assistant", "content": resposta})
    _trim(chat_id)
    return resposta


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
