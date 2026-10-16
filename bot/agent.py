"""O 'cérebro': conversa com o Claude e executa as ferramentas do Trello.

Mantém um histórico curto por chat (em memória). Cada mensagem do usuário
roda um laço: Claude pensa -> pode pedir ferramentas -> devolvemos o
resultado -> Claude responde em texto.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import anthropic

from . import config, costs, knowledge, memory, sitewatch, tools

log = logging.getLogger(__name__)

# timeout por chamada e poucas retentativas: evita que uma chamada fique presa
_client = anthropic.Anthropic(
    api_key=config.ANTHROPIC_API_KEY, timeout=120.0, max_retries=1
)

# Histórico por chat_id -> lista de mensagens (formato da API)
_history: dict[int, list[dict]] = {}

MAX_TURNS = 16  # trocas (falas do Állan + resposta) mantidas na RAM
KEEP_FULL_TURNS = 2  # só as 2 últimas trocas guardam os blocos de ferramenta
MAX_TOOL_LOOPS = 16  # análises com várias fotos/buscas podem precisar de mais rodadas

# Persistência do fio da conversa em disco (só o texto das trocas), para o bot
# continuar lembrando do assunto mesmo após reiniciar.
_SESS_DIR = os.path.join(os.path.dirname(config.MEMORY_FILE), "sessions")
MAX_DISK = 40  # últimas ~20 trocas (só texto) guardadas em disco


def _disk_path(chat_id: int) -> str:
    return os.path.join(_SESS_DIR, f"{chat_id}.json")


def _load_disk(chat_id: int) -> list[dict]:
    try:
        with open(_disk_path(chat_id), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_turn(chat_id: int, user_text: str, assistant_text: str) -> None:
    try:
        os.makedirs(_SESS_DIR, exist_ok=True)
        hist = _load_disk(chat_id)
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": assistant_text})
        hist = hist[-MAX_DISK:]
        with open(_disk_path(chat_id), "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _get_history(chat_id: int) -> list[dict]:
    """Pega o histórico da RAM; se vazio (ex: após reinício), carrega do disco."""
    msgs = _history.get(chat_id)
    if msgs is None:
        msgs = _load_disk(chat_id)
        _history[chat_id] = msgs
    return msgs


def _system_prompt(consulta: str = "") -> list[dict]:
    base = (
        "Você é o assistente pessoal do Állan, falando por Telegram em português "
        "do Brasil. Seja direto, prático e cordial — respostas curtas, sem enrolação. "
        "O Telegram renderiza **negrito** e listas com '- ' de verdade — use pra "
        "destacar totais/títulos e organizar visualmente, mas com moderação (não "
        "precisa negritar a mensagem toda).\n\n"
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
        "PLANEJAMENTO FINANCEIRO: fin_summary já devolve o total, o total por categoria "
        "(com % do total) e, se houver meta definida, o quanto já foi usado do orçamento "
        "dessa categoria — CONFIE nesses números prontos, NUNCA some/recalcule na mão a "
        "partir da lista de lançamentos (é assim que erros de conta acontecem). Se ele "
        "quiser ver os lançamentos crus, um por um (ex: conferir uma fatura linha a "
        "linha), use fin_list — não tente listar e somar tudo você mesmo numa mensagem só. "
        "Quando ele definir um limite de gasto por categoria ('quero gastar no máximo "
        "R$800 em lazer por mês'), salve com fin_budget_set; fin_budget_list mostra o que "
        "já foi definido. Ao comentar o financeiro, aponte proativamente categorias perto "
        "ou acima do orçamento e sugira ajustes — esse é o papel de conselheiro financeiro.\n\n"
        "REGRA DE OURO DOS REGISTROS: só diga 'registrado/anotado/salvo/agendado/apagado' se "
        "você CHAMOU a ferramenta correspondente NESTA resposta e ela deu certo. Se o Állan "
        "pediu pra registrar (refeição, treino, gasto, lembrete, evento), chame a ferramenta "
        "SEMPRE — mesmo que pareça repetido de antes: o histórico da conversa NÃO é o banco "
        "de dados. Se um resumo não mostra algo que ele acabou de mandar registrar, NÃO invente "
        "explicação (fuso, servidor, sincronização, data errada): diga com franqueza que não "
        "está salvo e registre agora.\n"
        "DATAS: cada fala do Állan vem com [dia da semana dd/mm hh:mm] no começo — é só "
        "referência (não repita nas respostas). 'Hoje', 'ontem' e 'amanhã' ditos em mensagens "
        "ANTIGAS do histórico se referem ao dia daquela mensagem; pra tudo que ele pedir agora, "
        "use sempre as datas de 'Agora' (hoje/ontem/anteontem) no fim deste prompt.\n\n"
        "CORRIGIR/APAGAR: você PODE editar e apagar registros. Para achar o item certo, "
        "liste primeiro (diet_summary / fin_summary / ex_summary / gcal_list_events) e use "
        "o #id mostrado. Dieta: diet_edit / diet_delete. Financeiro e exercícios: entry_edit "
        "/ entry_delete. Agenda: gcal_update_event / gcal_delete_event. Trello: "
        "trello_update_card. Antes de APAGAR algo, confirme rapidamente com ele.\n\n"
        "MEMÓRIA: quando o Állan contar algo DURÁVEL sobre ele (preferências, pessoas "
        "próximas, rotina, metas, restrições, contexto de trabalho), salve com memory_save "
        "sem alarde. Use a memória abaixo para personalizar suas respostas. Se ele pedir "
        "'o que você sabe sobre mim', use memory_show; para apagar, memory_forget.\n\n"
        "INTERNET: você pode PESQUISAR na web (web_search) e ABRIR páginas/links (web_fetch) "
        "quando precisar de informação atual (notícias, preços, cotações, horários, clima, "
        "pesquisa) ou pra resumir um link que ele mandar. Pesquise só quando agregar valor e "
        "cite a fonte de forma curta. Não use pra coisas que você já sabe.\n\n"
        "LOJAS/SITES (sites_status): as lojas do Állan são vigiadas por um painel de "
        "monitoramento, e quando uma CAI o aviso chega sozinho no Telegram (você não precisa "
        "fazer nada; ele não quer aviso de 'voltou', lentidão ou SSL). Se ele perguntar como estão os sites/lojas, se algum caiu "
        "ou como foi nas últimas horas, use sites_status. Não invente a causa de uma queda: "
        "o painel só sabe se respondeu e com qual erro.\n\n"
        "CUSTOS: se ele perguntar quanto gastou nas APIs, use cost_summary (por API: "
        "Anthropic e OpenAI). Lembre que é estimativa por uso.\n\n"
        "LEMBRETES/MENSAGENS PROGRAMADAS (reminder_*): quando ele pedir para você mandar "
        "uma mensagem depois ('me lembra de X às 15h', 'daqui a 30 min me avisa Y'), use "
        "reminder_create com when_iso calculado a partir do horário atual. Você mesmo vai "
        "mandar essa mensagem sozinho, sem o Állan perguntar nada. Para algo que se repete "
        "(aniversário, mensalidade, reunião semanal), use recurrence='yearly'/'monthly'/"
        "'weekly'/'daily' — when_iso é a primeira ocorrência, as próximas são automáticas. "
        "reminder_list mostra os pendentes; reminder_cancel cancela pelo id. Ligações de "
        "voz/vídeo pelo Telegram NÃO são possíveis (limitação da plataforma para bots) — se "
        "ele pedir, explique isso.\n\n"
"ARQUIVOS ANTERIORES (files_list/files_view): toda foto, PDF e documento que o Állan "
        "manda fica salvo. Se ele pedir para você REVER algo enviado antes (ex: 'olha de "
        "novo aquela foto da fatura', 'o PDF de ontem'), use files_list para achar o #id "
        "certo (por data/legenda/tipo) e depois files_view para reabrir — fotos e PDFs "
        "voltam como arquivo de verdade, você analisa de novo como se tivesse acabado de "
        "receber. Depois disso, aja normalmente com o que vir (ex: diet_log_meal, "
        "gcal_create_event, fin_log).\n\n"
        "RESPOSTA EM ÁUDIO: se o Állan pedir para você responder em áudio nesta mensagem "
        "(ex: 'manda em áudio', 'fala isso pra mim', 'responde por voz'), OU se isso estiver "
        "salvo como preferência permanente na memória dele, comece sua resposta final com a "
        "marca literal [AUDIO] bem no início (sem espaço antes), seguida do texto que você "
        "quer que seja falado — escreva como fala, sem markdown/emojis em excesso. Se ele "
        "disser para voltar a responder em texto, pare de usar a marca.\n\n"
        "NUNCA DIGA QUE NÃO LEMBRA sem consultar antes: o que o Állan já te contou/registrou "
        "está na memória (abaixo), na base de conhecimento (índice e material relevante "
        "abaixo, ou knowledge_read), nos registros (diet_summary, ex_summary, fin_summary, "
        "reminder_list) ou na agenda/Trello. Se a pergunta toca num assunto salvo e o "
        "conteúdo não veio em 'Material salvo relevante', chame knowledge_read ANTES de "
        "responder — uma consulta a mais custa muito menos que ele ter que repetir tudo. "
        "Pra um intervalo de dias, faça UMA chamada com start_date/end_date (nunca uma "
        "chamada por dia).\n\n"
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
    # Bloco estável (cacheado) + bloco volátil (hora + memória, não cacheado).
    # Isso reduz muito o custo: o "manual" repetido é reaproveitado a ~10%.
    dt = _agora()
    ontem, anteontem = dt - timedelta(days=1), dt - timedelta(days=2)
    volatil = (
        f"Agora: {_DIAS[dt.weekday()]}, {dt:%d/%m/%Y %H:%M} ({config.TIMEZONE}). "
        f"Hoje = {dt:%d/%m/%Y}; ontem = {ontem:%d/%m/%Y} ({_DIAS[ontem.weekday()]}); "
        f"anteontem = {anteontem:%d/%m/%Y}."
    )
    perfil = memory.load()
    if perfil:
        volatil += "\n\n## O que você já sabe sobre o Állan (memória)\n" + perfil
    try:
        indice = knowledge.index()
        if indice:
            volatil += "\n\n## Assuntos salvos na base de conhecimento\n" + indice
        material = knowledge.relevant(consulta) if consulta else ""
        if material:
            volatil += (
                "\n\n## Material salvo relevante para esta mensagem (já consultado — "
                "use direto, sem pedir de novo)\n" + material
            )
    except Exception:  # noqa: BLE001
        log.exception("Falha ao montar o contexto da base de conhecimento")
    if config.SITEWATCH_ENABLED:
        try:
            avisos = sitewatch.avisos_recentes()
            if avisos:
                volatil += (
                    "\n\n## Avisos automáticos de sites que você mandou ao Állan nas últimas 24h "
                    "(se ele comentar ou perguntar sobre um aviso, é disso que se trata)\n" + avisos
                )
        except Exception:  # noqa: BLE001
            log.exception("Falha ao ler os avisos do monitor de sites")
    return [
        {"type": "text", "text": base, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatil},
    ]


def reset(chat_id: int) -> None:
    _history.pop(chat_id, None)
    try:
        os.remove(_disk_path(chat_id))
    except FileNotFoundError:
        pass


_AUDIO_MARK = "[AUDIO]"

# Sinais de que a mensagem de texto pede uma análise pesada/detalhada —
# nesse caso usa o modelo completo em vez do econômico, mesmo sendo só texto.
_PALAVRAS_PESADAS = (
    "recalcul", "detalhad", "linha por linha", "item por item",
    "análise completa", "com precisão", "revisa tudo", "revê tudo",
    "todos os itens", "fatura inteira", "extrato inteiro", "conferir a fatura",
)


def _escolher_modelo(texto: str) -> str:
    """Mensagens de texto do dia a dia (registrar gasto/refeição, marcar
    compromisso, perguntas rápidas) vão pro modelo econômico. Pedidos de
    análise pesada/detalhada, ou mensagens muito longas (ex: texto colado),
    usam o modelo completo."""
    if not config.HYBRID_MODEL:
        return config.MODEL
    t = (texto or "").lower()
    if len(texto or "") > 500:
        return config.MODEL
    if any(p in t for p in _PALAVRAS_PESADAS):
        return config.MODEL
    return config.MODEL_LITE


def _split_audio_marker(raw: str) -> tuple[str, bool]:
    """Detecta a marca [AUDIO] no início da resposta do Claude e a remove."""
    if raw.startswith(_AUDIO_MARK):
        return raw[len(_AUDIO_MARK):].strip(), True
    return raw, False


_DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def _agora() -> datetime:
    return datetime.now(ZoneInfo(config.TIMEZONE))


def _carimbo(dt: datetime | None = None) -> str:
    """'qui 15/10 22:28' — vai no começo de cada fala do Állan no histórico. Numa
    conversa que atravessa a meia-noite, 'hoje'/'ontem' ditos antes passam a
    significar outro dia; com o carimbo o bot não se confunde (foi o que
    aconteceu em 15→16/10: ele consultou o dia 14 quando 'ontem' era o 15)."""
    dt = dt or _agora()
    return f"{_DIAS[dt.weekday()][:3]} {dt:%d/%m %H:%M}"


# Pedido de AÇÃO em imperativo ("registra o almoço", "anota isso", "apaga aquele").
_PEDIDO_ACAO = re.compile(
    r"\b(" + "|".join(
        "registra registre registrar anota anote anotar lança lance lançar lanca adiciona "
        "adicione adicionar acrescenta acrescente acrescentar coloca coloque colocar inclui "
        "inclua incluir salva salve salvar agenda agende agendar apaga apague apagar remove "
        "remova remover exclui exclua excluir corrige corrija corrigir atualiza atualize "
        "atualizar".split()
    ) + r")\b",
    re.I,
)
# O bot AFIRMANDO que fez uma ação ("Registrado!", "Anotei", "Agendado").
_CONFIRMA_ACAO = re.compile(
    r"\b(registrad[oa]s?|registrei|anotad[oa]s?|anotei|lançad[oa]s?|lancei|salvei|salvad[oa]s?|"
    r"adicionad[oa]s?|adicionei|acrescentad[oa]s?|acrescentei|agendad[oa]s?|agendei|"
    r"apagad[oa]s?|apaguei|removid[oa]s?|removi|exclu[ií]d[oa]s?|atualizad[oa]s?|atualizei|"
    r"corrigid[oa]s?|corrigi)\b",
    re.I,
)


def handle_message(
    chat_id: int,
    text: str,
    image_bytes: bytes | None = None,
    media_type: str = "image/jpeg",
) -> tuple[str, bool]:
    """Processa uma mensagem do usuário (texto e/ou imagem). Devolve (resposta,
    quer_audio) — quer_audio indica se deve ser enviada como voz."""
    msgs = _get_history(chat_id)
    carimbo = f"[{_carimbo()}] "  # dia/hora da fala, p/ 'hoje'/'ontem' não se confundirem

    img_idx = -1
    if image_bytes:
        # Foto: sempre modelo completo — estimar caloria/ler evento numa imagem
        # exige mais qualidade de visão do que vale a pena arriscar no econômico.
        modelo = config.MODEL
        b64 = base64.standard_b64encode(image_bytes).decode()
        legenda = carimbo + (text or (
            "Analise esta imagem. Se for comida, registre a refeição; se for um "
            "evento/convite, crie na agenda. Se faltar dado, me pergunte."
        ))
        img_idx = len(msgs)
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
        modelo = _escolher_modelo(text)
        msgs.append({"role": "user", "content": carimbo + text})

    raw = _run(msgs, chat_id, modelo)
    resposta, quer_audio = _split_audio_marker(raw)

    # Depois de processada, troca a imagem (pesada) por uma nota curta no
    # histórico — evita reenviar a foto nas próximas mensagens.
    if img_idx >= 0:
        msgs[img_idx] = {"role": "user", "content": carimbo + "[Enviei uma foto para você analisar.]"}

    _save_turn(chat_id, carimbo + (text or "[foto]"), resposta)
    _trim(chat_id)
    return resposta, quer_audio


def _run(msgs: list[dict], chat_id: int, model: str | None = None) -> str:
    """Executa o laço de ferramentas sobre a lista de mensagens dada.

    Se algo der errado no meio (limite de passos ou exceção), desfaz tudo que
    essa tentativa adicionou a `msgs` — senão um bloco de ferramenta do
    servidor (ex: execução de código) pode ficar 'pendurado' sem resposta, e
    toda mensagem seguinte passa a dar erro 400 (histórico corrompido).
    """
    modelo = model or config.MODEL
    checkpoint = len(msgs)
    # Últimas falas do Állan (incluindo a atual): usadas pra trazer, já no
    # contexto, o material salvo que elas tocam (ex: "abastecimento" → carro).
    consulta = _consulta_de(msgs)
    # Trava contra "Registrado!" sem registrar (aconteceu em 15/10: 3 refeições
    # confirmadas pelo bot e nenhuma gravada). Se o Állan pediu uma AÇÃO e o bot
    # responde dizendo que fez, sem ter chamado nenhuma ferramenta, a resposta é
    # descartada e refeita UMA vez obrigando o uso de ferramenta.
    pediu_acao = bool(_PEDIDO_ACAO.search(_consulta_de(msgs, 1)))
    chamou_tool = False
    retentou = False
    forcar_tool = False
    try:
        for i in range(MAX_TOOL_LOOPS):
            # Rede de segurança: se o modelo econômico não resolveu em várias
            # rodadas de ferramenta, a tarefa provavelmente é mais complexa do
            # que a mensagem parecia — escala pro modelo completo no meio da
            # mesma conversa, em vez de continuar insistindo ou travar.
            if modelo == config.MODEL_LITE and i == 4:
                log.info("Escalando pro modelo completo no meio do laço (chat=%s)", chat_id)
                modelo = config.MODEL

            kwargs = dict(
                model=modelo,
                max_tokens=8192,
                system=_system_prompt(consulta),
                tools=tools.TOOLS,
                messages=msgs,
            )
            if not modelo.startswith("claude-haiku"):
                # "effort" (esforço de raciocínio) só existe no Sonnet/Opus;
                # o Haiku rejeita esse parâmetro com erro 400.
                kwargs["output_config"] = {"effort": "low"}
            if forcar_tool:
                kwargs["tool_choice"] = {"type": "any"}  # só nesta chamada: PRECISA usar ferramenta
                forcar_tool = False
            resp = _client.messages.create(**kwargs)
            costs.record_anthropic(resp.usage, modelo)
            msgs.append({"role": "assistant", "content": resp.content})
            log.info("API modelo=%s stop=%s blocos=%s [chat=%s]",
                     modelo, resp.stop_reason, [b.type for b in resp.content], chat_id)

            if resp.stop_reason == "tool_use":
                chamou_tool = True
                resultados = []
                for bloco in resp.content:
                    if bloco.type == "tool_use":
                        # Log leve de qual ferramenta foi chamada — sem isso, fica
                        # impossível diagnosticar depois um caso de "não lembrou"
                        # (saber se ele nem tentou consultar, ou consultou errado).
                        log.info("Tool %s(%s) [chat=%s, modelo=%s]", bloco.name, bloco.input or {}, chat_id, modelo)
                        saida = tools.run_tool(bloco.name, bloco.input or {}, chat_id)
                        resultados.append(
                            {"type": "tool_result", "tool_use_id": bloco.id, "content": saida}
                        )
                msgs.append({"role": "user", "content": resultados})
                continue

            # Ferramentas do servidor (busca web/execução de código) podem
            # pausar; reenvia pra continuar de onde parou
            if resp.stop_reason == "pause_turn":
                chamou_tool = True
                continue

            texto = "".join(b.text for b in resp.content if b.type == "text").strip()

            if resp.stop_reason == "max_tokens":
                # Estourou o limite de tokens no meio da resposta (comum em
                # pedidos de detalhamento muito grande, ex: recalcular dezenas
                # de itens de uma vez). Se sobrou algo de texto, devolve com
                # aviso; se não sobrou nada (gastou tudo "pensando"), pede pra
                # dividir o pedido em vez de mostrar uma resposta muda.
                log.warning(
                    "max_tokens atingido (chat=%s, modelo=%s, texto_parcial=%d chars, usage=%s)",
                    chat_id, modelo, len(texto), resp.usage,
                )
                del msgs[checkpoint:]  # turno incompleto — não deixa lixo no histórico
                if texto:
                    return texto + "\n\n_(cortei aqui por tamanho — me chama que eu continuo)_"
                return (
                    "Essa resposta ficou grande demais e travei no meio, sem conseguir "
                    "escrever nada. Pode pedir de novo dividindo em partes menores? "
                    "(ex: primeiro só o total corrigido, depois o detalhamento item por item)"
                )

            if not texto:
                log.warning(
                    "Resposta sem texto (chat=%s, modelo=%s, stop_reason=%s, blocos=%s, usage=%s)",
                    chat_id, modelo, resp.stop_reason,
                    [b.type for b in resp.content], resp.usage,
                )
                del msgs[checkpoint:]  # turno vazio/quebrado — não deixa lixo no histórico
                if modelo == config.MODEL_LITE:
                    # Fraqueza conhecida do modelo econômico: às vezes devolve
                    # uma resposta vazia logo depois de usar uma ferramenta.
                    # Tenta de novo, uma vez, com o modelo completo — invisível
                    # pro Állan na maioria das vezes, em vez de desistir.
                    log.info("Modelo econômico devolveu vazio; tentando de novo com o completo (chat=%s)", chat_id)
                    return _run(msgs, chat_id, config.MODEL)
                return "Não consegui formular uma resposta agora. Pode tentar reformular?"

            if pediu_acao and not chamou_tool and not retentou and _CONFIRMA_ACAO.search(texto):
                log.warning(
                    "Resposta confirma ação SEM chamar ferramenta — refazendo com tool_choice=any "
                    "(chat=%s, modelo=%s): %r", chat_id, modelo, texto[:160],
                )
                del msgs[checkpoint:]  # descarta a resposta mentirosa; o histórico não aprende com ela
                retentou = True
                forcar_tool = True
                continue

            return texto

        # Excedeu o limite de passos: pode haver um bloco de ferramenta do
        # servidor sem resultado ainda — desfaz para não corromper o histórico
        del msgs[checkpoint:]
        return "Precisei de muitos passos e parei por segurança. Pode reformular?"
    except Exception:
        del msgs[checkpoint:]
        raise


def handle_document(
    chat_id: int, filename: str, content: str, instruction: str = ""
) -> tuple[str, bool]:
    """Lê um documento anexado (ex: export de conversa), resume e aprende — sem
    guardar o texto gigante no histórico."""
    msgs = _get_history(chat_id)
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

    # Roda numa cópia do histórico para NÃO persistir o texto gigante.
    # Documento: sempre modelo completo (análise/resumo pede mais qualidade).
    work = msgs + [{"role": "user", "content": prompt}]
    raw = _run(work, chat_id, config.MODEL)
    resposta, quer_audio = _split_audio_marker(raw)

    # No histórico real, guarda só um registro compacto + a resposta
    msgs.append(
        {"role": "user", "content": f"[{_carimbo()}] [Enviei o documento '{filename}' e pedi um resumo/análise.]"}
    )
    msgs.append({"role": "assistant", "content": resposta})
    _save_turn(chat_id, f"[{_carimbo()}] [documento: {filename}]", resposta)
    _trim(chat_id)
    return resposta, quer_audio


def handle_pdf(
    chat_id: int, filename: str, pdf_bytes: bytes, instruction: str = ""
) -> tuple[str, bool]:
    """Lê um PDF anexado usando a leitura nativa de PDF do Claude (funciona até
    com PDF escaneado/imagem). Não guarda o base64 gigante no histórico real."""
    msgs = _get_history(chat_id)
    pedido = instruction.strip() or (
        "Leia este PDF e resuma o que for relevante. Se for fatura/extrato, aponte "
        "valores e vencimentos; se houver algo durável sobre mim, memorize com "
        "memory_save."
    )
    b64 = base64.standard_b64encode(pdf_bytes).decode()
    prompt_content = [
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        },
        {"type": "text", "text": f"PDF anexado: {filename}\n{pedido}"},
    ]

    # Roda numa cópia do histórico para NÃO persistir o PDF gigante.
    # PDF: sempre modelo completo (fatura/extrato pede precisão de leitura).
    work = msgs + [{"role": "user", "content": prompt_content}]
    raw = _run(work, chat_id, config.MODEL)
    resposta, quer_audio = _split_audio_marker(raw)

    # No histórico real, guarda só um registro compacto + a resposta
    msgs.append(
        {"role": "user", "content": f"[{_carimbo()}] [Enviei o PDF '{filename}' e pedi um resumo/análise.]"}
    )
    msgs.append({"role": "assistant", "content": resposta})
    _save_turn(chat_id, f"[{_carimbo()}] [pdf: {filename}]", resposta)
    _trim(chat_id)
    return resposta, quer_audio


def _is_tool_result_only(msg: dict) -> bool:
    content = msg.get("content")
    if isinstance(content, list):
        return all(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def _is_turn_start(msg: dict) -> bool:
    """Mensagem em que o Állan realmente fala (não o retorno de uma ferramenta)."""
    return msg.get("role") == "user" and not _is_tool_result_only(msg)


def _texto_de(msg: dict) -> str:
    """Só o texto de uma mensagem (usuário ou assistente), sem blocos de ferramenta."""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    partes = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                partes.append(b.get("text", ""))
        elif getattr(b, "type", "") == "text":
            partes.append(b.text)
    return "".join(partes).strip()


def _consulta_de(msgs: list[dict], quantas: int = 3) -> str:
    """Junta as últimas falas do Állan — serve de 'pergunta' pra buscar o material
    salvo relevante (inclui as anteriores pra acompanhar 'e na cidade?')."""
    falas = [_texto_de(m) for m in msgs if _is_turn_start(m)]
    return "\n".join(f for f in falas[-quantas:] if f)


def _compact_history(msgs: list[dict]) -> list[dict]:
    """Mantém as últimas MAX_TURNS trocas. As antigas viram só 'o que o Állan disse
    + resposta final' (sem blocos de ferramenta, que ocupam espaço e custam token);
    as KEEP_FULL_TURNS mais recentes ficam completas (ids, resultados). Assim cabe
    muito mais conversa do mesmo dia no mesmo custo — antes, cada troca com
    ferramenta gastava 3-4 das 12 mensagens guardadas (≈3 trocas de memória)."""
    inicios = [i for i, m in enumerate(msgs) if _is_turn_start(m)]
    if not inicios:
        return []
    fins = inicios[1:] + [len(msgs)]
    trocas = [msgs[a:b] for a, b in zip(inicios, fins)][-MAX_TURNS:]

    out: list[dict] = []
    for idx, troca in enumerate(trocas):
        if idx >= len(trocas) - KEEP_FULL_TURNS:
            out.extend(troca)
            continue
        fala = _texto_de(troca[0])
        resposta = ""
        for m in reversed(troca):
            if m.get("role") == "assistant":
                resposta = _texto_de(m)
                if resposta:
                    break
        if fala and resposta:  # troca sem resposta (falhou) é descartada
            out.append({"role": "user", "content": fala})
            out.append({"role": "assistant", "content": resposta})
    return out


def _trim(chat_id: int) -> None:
    msgs = _history.get(chat_id)
    if msgs:
        _history[chat_id] = _compact_history(msgs)
