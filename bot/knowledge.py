"""Base de conhecimento: pasta de arquivos .md que o bot consulta e amplia.

Cada arquivo é um 'assunto' (ex: cupido, vendas, investimentos). O bot lista/lê
quando o tema é relevante, e o Állan pode ensinar coisas novas a qualquer momento
(o bot salva com knowledge_save). É assim que o assistente fica mais inteligente
com o tempo.
"""
from __future__ import annotations

import os
import re
import unicodedata

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


# ---------------------------------------------------------------------------
# Consulta automática: traz o assunto certo pro contexto SEM depender do modelo
# lembrar de chamar knowledge_read (era a causa de "esquecer" o abastecimento).
# ---------------------------------------------------------------------------

_STOP = {
    "quando", "qual", "quais", "quanto", "quantos", "quanta", "como", "onde", "porque",
    "ultimo", "ultima", "ultimos", "primeiro", "foi", "fiz", "fez", "fui", "meu", "minha",
    "meus", "minhas", "para", "pelo", "pela", "isso", "esse", "essa", "esta", "este",
    "aqui", "tem", "tinha", "uma", "umas", "uns", "com", "dos", "das", "que", "nao",
    "por", "mais", "voce", "ainda", "sobre", "hoje", "ontem", "agora", "depois", "antes",
    "pode", "quero", "queria", "preciso", "lembra", "lembrar", "lembro", "saber", "disse",
    "falei", "falou", "mandei", "registrei", "registrar", "registro", "anotei", "salvei",
    "algo", "coisa", "coisas", "tudo", "todo", "toda", "todos", "muito", "pouco", "bem",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _stems(texto: str) -> set[str]:
    """Radicais curtos (5 letras, sem plural) — 'abastecimento', 'abasteci' e
    'abastecer' viram todos 'abast'."""
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9]+", _norm(texto)):
        if len(w) < 4 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        out.add(w[:5])
    return out


def _load_all() -> dict[str, str]:
    d = _dir()
    out: dict[str, str] = {}
    for f in sorted(os.listdir(d)):
        if f.endswith(".md"):
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    out[f[:-3]] = fh.read().strip()
            except OSError:
                pass
    return out


def index() -> str:
    """Índice curto dos assuntos (nome + 1ª linha descritiva), p/ ir no prompt."""
    linhas = []
    for nome, conteudo in _load_all().items():
        desc = next(
            (l.strip() for l in conteudo.splitlines() if l.strip() and not l.startswith("#")),
            "",
        )
        linhas.append(f"- {nome}" + (f" — {desc[:90]}" if desc else ""))
    return "\n".join(linhas)


def relevant(texto: str, limite_total: int = 6000, limite_topico: int = 3500) -> str:
    """Devolve o conteúdo dos assuntos que a mensagem toca (pelo nome do assunto,
    ou por palavras que só aparecem em 1-2 assuntos). Vazio se nada bater."""
    topicos = _load_all()
    q = _stems(texto)
    if not topicos or not q:
        return ""
    por_topico = {n: _stems(c) for n, c in topicos.items()}
    freq: dict[str, int] = {}
    for st in por_topico.values():
        for s in st:
            freq[s] = freq.get(s, 0) + 1

    achados = []
    for nome, conteudo in topicos.items():
        hit_nome = q & _stems(nome.replace("-", " ").replace("_", " "))
        hit_conteudo = {s for s in q & por_topico[nome] if freq[s] <= 2}
        score = len(hit_nome) * 3 + len(hit_conteudo)
        if score:
            achados.append((score, nome, conteudo))
    achados.sort(key=lambda x: -x[0])

    blocos, usado = [], 0
    for _score, nome, conteudo in achados:
        if len(conteudo) > limite_topico:  # diário cresce no fim: guarda começo + fim
            conteudo = conteudo[:400] + "\n[... trecho antigo omitido ...]\n" + conteudo[-(limite_topico - 400):]
        if usado + len(conteudo) > limite_total:
            break
        blocos.append(f"### {nome}\n{conteudo}")
        usado += len(conteudo)
    return "\n\n".join(blocos)


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
