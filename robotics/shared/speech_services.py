#!/usr/bin/env python3
"""
speech_services.py — Transcription (Whisper ASR) et synthèse vocale (edge-tts)
partagées par les drivers de corps (naoqi_driver, static_driver).

Les deux appels sont bloquants : les nœuds ROS 2 les lancent dans un thread
pour ne pas bloquer la boucle rclpy.

    text = transcribe(WHISPER_URL, audio_bytes)     # lève WhisperError
    if is_meaningful(text): ...
    mp3 = synthesize_mp3(text, "fr-FR-DeniseNeural") # lève ImportError si edge-tts absent
"""

import asyncio
import io

import requests


class WhisperError(Exception):
    """Réponse HTTP en erreur du service Whisper ASR."""


def transcribe(whisper_url: str, audio_bytes: bytes, language: str = "fr",
               timeout: float = 30, prompt: str = "") -> str:
    """Envoie un fichier audio à Whisper ASR et renvoie le texte (sans espaces autour).

    Le service onerahmet/openai-whisper-asr-webservice décode l'audio avec
    ffmpeg : WAV, MP3, etc. sont acceptés.  `prompt` (initial_prompt de
    Whisper) oriente l'orthographe : "Ok Nao." aide à écrire le mot de réveil.
    """
    params = {"task": "transcribe", "language": language, "output": "json"}
    if prompt:
        params["initial_prompt"] = prompt
    resp = requests.post(
        f"{whisper_url}/asr",
        params=params,
        files={"audio_file": ("audio.wav", audio_bytes, "audio/wav")},
        timeout=timeout,
    )
    # Whisper returned an HTTP error — the caller logs it and gives up.
    if not resp.ok:
        raise WhisperError(f"Whisper erreur {resp.status_code}")
    return resp.json().get("text", "").strip()


# Ce que Whisper invente sur du bruit ou un silence (sous-titres de vidéos).
HALLUCINATIONS = ("sous-titr", "merci d'avoir regard", "abonnez-vous", "amara.org")


def is_meaningful(text: str) -> bool:
    """Accept transcription only if it contains meaningful content
    (> 3 chars filters out noise artefacts like "." or "ok", and the usual
    Whisper hallucinations on noise are dropped)."""
    if not text or len(text) <= 3:
        return False
    lower = text.lower()
    return not any(h in lower for h in HALLUCINATIONS)


def synthesize_mp3(text: str, voice: str) -> bytes:
    """Génère un MP3 avec edge-tts. Lève ImportError si edge-tts n'est pas installé."""
    import edge_tts

    async def _gen():
        communicate = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            # Only audio chunks contain binary data — skip metadata events.
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_gen())
    finally:
        loop.close()
