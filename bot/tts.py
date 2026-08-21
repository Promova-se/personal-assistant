"""Síntese de voz (texto -> áudio) via OpenAI TTS.

Usa a mesma chave da transcrição (Whisper). Devolve OGG/Opus, formato que o
Telegram aceita direto como mensagem de voz.
"""
from __future__ import annotations

import requests

from . import config

URL = "https://api.openai.com/v1/audio/speech"
MODEL = "tts-1"
VOICE = "alloy"
MAX_CHARS = 4000  # limite da API


def synthesize(text: str) -> bytes:
    if not config.AUDIO_ENABLED:
        raise RuntimeError("OPENAI_API_KEY não configurada")
    r = requests.post(
        URL,
        headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
        json={
            "model": MODEL,
            "voice": VOICE,
            "input": text[:MAX_CHARS],
            "response_format": "opus",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.content
