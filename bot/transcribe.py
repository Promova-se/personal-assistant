"""Transcrição de áudio via API da OpenAI (Whisper).

Recebe os bytes do áudio (OGG/Opus do Telegram) e devolve o texto.
Não usa SDK: um POST multipart simples pro endpoint de transcrição.
"""
from __future__ import annotations

import requests

from . import config

URL = "https://api.openai.com/v1/audio/transcriptions"


def transcribe(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    if not config.AUDIO_ENABLED:
        return ""
    r = requests.post(
        URL,
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        files={"file": (filename, audio_bytes, "audio/ogg")},
        data={"model": "whisper-1", "language": "pt"},
        timeout=120,
    )
    r.raise_for_status()
    return (r.json().get("text") or "").strip()
