"""
title: NAO TTS
author: ia-agent
version: 0.6.0
description: >
  Filtre de sortie — envoie chaque réponse LLM à NAO pour qu'il la dise
  à haute voix via ALTextToSpeech.

  Parcours 1 (texte WebUI) :
    L'utilisateur écrit dans le chat → le LLM génère une réponse naturelle
    (affichée normalement dans WebUI) → ce filtre l'attrape → body-gateway
    /say → ROS2 /nao/speech → rosbridge → NAO dit la réponse à voix haute.

  Parcours 2 (voix "Ok nao") :
    Le robot transcrit la parole → body-gateway injecte le texte dans WebUI
    → le LLM génère la réponse → ce même filtre envoie le TTS à NAO.
    La transcription et la réponse apparaissent dans le chat NAO Voice.

  Installation dans Open WebUI :
    Workspace → Functions → (+) → coller ce fichier → Enregistrer
    Puis activer le filtre sur le(s) modèle(s) souhaités :
    Workspace → Models → <modèle> → Filters → NAO TTS → On

required_open_webui_version: 0.3.0
"""

import re

import requests
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        body_gateway_url: str = Field(
            default="http://chat-relay:8095",
            description=(
                "URL du chat-relay (proxy Caddy sur ai-network → body-gateway:8095 sur l'hôte)."
            ),
        )
        robot_id: str = Field(
            default="nao_01",
            description="Corps principal ciblé par défaut (doit être dans BODY_REGISTRY).",
        )

    def __init__(self):
        self.valves = self.Valves()

    def outlet(self, body: dict, __user__: dict = {}) -> dict:
        """Intercepte la réponse LLM et la transmet à NAO pour TTS."""
        text = self._extract_response(body.get("messages", []))
        text = self._strip_markdown(text)
        if text:
            try:
                requests.post(
                    f"{self.valves.body_gateway_url}/say",
                    json={"text": text, "robot_id": self.valves.robot_id},
                    timeout=5,
                )
            except requests.exceptions.ConnectionError:
                pass  # body-gateway indisponible — TTS ignoré silencieusement
            except Exception:
                pass
        return body

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _extract_response(self, messages: list) -> str:
        """Retourne le contenu texte du dernier message assistant."""
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, list):
                    return "".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                return content or ""
        return ""

    def _strip_markdown(self, text: str) -> str:
        """Retire le formatage Markdown pour un TTS lisible."""
        if not text:
            return ""
        # Blocs de code
        text = re.sub(r"```[\s\S]*?```", "", text)
        text = re.sub(r"`[^`\n]+`", "", text)
        # Gras / italique
        text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
        text = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", text)
        # Titres
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        # Liens et images
        text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        # Listes
        text = re.sub(r"^[ \t]*[-*+]\s+", "", text, flags=re.MULTILINE)
        text = re.sub(r"^[ \t]*\d+\.\s+", "", text, flags=re.MULTILINE)
        # Sauts de ligne multiples → espace
        text = re.sub(r"\n+", " ", text)
        text = re.sub(r"\s{2,}", " ", text)
        return text.strip()
