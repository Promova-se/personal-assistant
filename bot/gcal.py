"""Ferramentas da Google Agenda via conta de serviço.

A conta de serviço acessa a agenda do usuário porque ela foi compartilhada
(nas configurações do Google Agenda) com o e-mail da conta de serviço.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from . import config

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None
_TZ = ZoneInfo(config.TIMEZONE)


def _svc():
    global _service
    if _service is None:
        creds = service_account.Credentials.from_service_account_file(
            config.GCAL_KEYFILE, scopes=SCOPES
        )
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def _fmt(ev: dict) -> str:
    start = ev.get("start", {})
    quando = start.get("dateTime") or start.get("date") or "?"
    # Deixa o horário mais legível se vier ISO com fuso
    try:
        dt = datetime.fromisoformat(quando.replace("Z", "+00:00")).astimezone(_TZ)
        quando = dt.strftime("%d/%m %H:%M")
    except ValueError:
        pass  # evento de dia inteiro (só data)
    titulo = ev.get("summary", "(sem título)")
    local = ev.get("location")
    extra = f" @ {local}" if local else ""
    return f"{quando} — {titulo}{extra} (id: {ev.get('id')})"


def list_events(days: int = 1, start_iso: str = "", end_iso: str = "") -> str:
    """Lista eventos. Por padrão, de agora até 'days' dias à frente."""
    agora = datetime.now(_TZ)
    time_min = start_iso or agora.isoformat()
    time_max = end_iso or (agora + timedelta(days=days)).isoformat()
    res = (
        _svc()
        .events()
        .list(
            calendarId=config.GCAL_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )
    itens = res.get("items", [])
    if not itens:
        return "Nenhum evento nesse período."
    return "Eventos:\n" + "\n".join("- " + _fmt(e) for e in itens)


def create_event(
    summary: str,
    start_iso: str,
    end_iso: str = "",
    description: str = "",
    location: str = "",
) -> str:
    """Cria um evento. start_iso/end_iso em ISO 8601 com fuso.
    Se end_iso vazio, dura 1 hora."""
    if not end_iso:
        dt = datetime.fromisoformat(start_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ)
        end_iso = (dt + timedelta(hours=1)).isoformat()
    body = {
        "summary": summary,
        "start": {"dateTime": start_iso, "timeZone": config.TIMEZONE},
        "end": {"dateTime": end_iso, "timeZone": config.TIMEZONE},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    ev = (
        _svc()
        .events()
        .insert(calendarId=config.GCAL_CALENDAR_ID, body=body)
        .execute()
    )
    return f"Evento criado: {_fmt(ev)}\n{ev.get('htmlLink', '')}"


def delete_event(event_id: str) -> str:
    _svc().events().delete(
        calendarId=config.GCAL_CALENDAR_ID, eventId=event_id
    ).execute()
    return f"Evento {event_id} removido."


# ---------------------------------------------------------------------------
# Esquemas para o Claude
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "gcal_list_events",
        "description": (
            "Lista eventos da Google Agenda. Use days para a janela a partir de "
            "agora (1 = próximas 24h, 7 = semana). Ou passe start_iso/end_iso "
            "(ISO 8601) para um intervalo específico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "Dias à frente a partir de agora"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
            },
        },
    },
    {
        "name": "gcal_create_event",
        "description": (
            "Cria um evento na Google Agenda. start_iso em ISO 8601 (ex: "
            "2026-08-12T14:00:00-03:00). Se não informar end_iso, dura 1 hora."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Título do evento"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["summary", "start_iso"],
        },
    },
    {
        "name": "gcal_delete_event",
        "description": "Remove um evento da Google Agenda pelo seu id.",
        "input_schema": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
    },
]

DISPATCH = {
    "gcal_list_events": lambda a: list_events(
        a.get("days", 1), a.get("start_iso", ""), a.get("end_iso", "")
    ),
    "gcal_create_event": lambda a: create_event(
        a["summary"],
        a["start_iso"],
        a.get("end_iso", ""),
        a.get("description", ""),
        a.get("location", ""),
    ),
    "gcal_delete_event": lambda a: delete_event(a["event_id"]),
}
