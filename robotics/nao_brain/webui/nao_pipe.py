"""
title: NAO Brain
author: ia-agent
version: 0.7.0
description: >
  Pipe cerveau — relaie chaque message WebUI vers body-gateway qui appelle
  le LLM avec un prompt JSON structuré. La réponse contient :
    • speech  : texte que NAO dit à voix haute ET affiché dans le chat
    • actions : gestes, LEDs, déplacement dispatché sur le robot

  Parcours 1 (texte) :
    Utilisateur écrit → Pipe → body-gateway /chat → Ollama → JSON
    → robot parle + agit → WebUI affiche "NAO : [speech] + Actions"

  Parcours 2 (voix "Ok nao") :
    NAO transcrit → body-gateway inférence → robot parle + agit
    → body-gateway stocke dans voice-queue → notifie WebUI
    → ce Pipe récupère le résultat pré-calculé via /voice-pop
    → WebUI affiche transcription + réponse

  Installation dans Open WebUI :
    Workspace → Functions → (+) → coller ce fichier → Enregistrer
    Puis sélectionner "NAO Brain" comme modèle dans le chat.

required_open_webui_version: 0.3.0
"""

import json

import requests
from pydantic import BaseModel, Field


class Pipe:
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
        self.type = "pipe"
        self.id = "nao_brain"
        self.name = "NAO Brain"

    def pipe(self, body: dict, __user__: dict = {}) -> str:
        messages = body.get("messages", [])
        if not messages:
            return "Aucun message."

        # Prend le dernier message utilisateur (Open WebUI peut ajouter un
        # assistant vide en fin de tableau lors de l'appel du pipe)
        user_msg = next(
            (m for m in reversed(messages) if m.get("role") == "user"),
            None,
        )
        if not user_msg:
            return "Aucun message utilisateur."

        content = user_msg.get("content", "")
        # Open WebUI ≥ 0.4 peut envoyer le contenu comme liste de blocs
        if isinstance(content, list):
            user_message = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            user_message = content

        if not user_message:
            return "Message vide."

        # Parcours 2 — résultat pré-calculé par body-gateway (voix "Ok nao")
        if body.get("nao_voice"):
            return self._handle_voice(user_message)

        # Parcours 1 — message texte WebUI
        return self._call_gateway(user_message)

    # ─────────────────────────────────────────────────────────────────────────
    # Parcours 2 — récupération du résultat pré-calculé (évite double inférence)
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_voice(self, transcription: str) -> str:
        try:
            r = requests.get(
                f"{self.valves.body_gateway_url}/voice-pop",
                timeout=5,
            )
            if r.ok:
                data = r.json()
                if data:
                    return f"*🎤 « {transcription} »*\n\n" + self._format(data)
        except Exception:
            pass
        # Fallback : traiter la transcription comme un message texte normal
        return self._call_gateway(transcription)

    # ─────────────────────────────────────────────────────────────────────────
    # Parcours 1 — appel body-gateway /chat → Ollama (JSON structuré)
    # ─────────────────────────────────────────────────────────────────────────

    def _call_gateway(self, text: str) -> str:
        try:
            r = requests.post(
                f"{self.valves.body_gateway_url}/chat",
                json={"text": text, "robot_id": self.valves.robot_id},
                timeout=(10, None),
            )
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.ConnectionError:
            return (
                "❌ body-gateway inaccessible — "
                "le stack robotique (`docker compose up -d`) est-il démarré ?"
            )
        except Exception as e:
            return f"❌ Erreur : {e}"

        return self._format(data)

    # ─────────────────────────────────────────────────────────────────────────
    # Formatage de la réponse body-gateway
    # ─────────────────────────────────────────────────────────────────────────

    def _format(self, data: dict) -> str:
        speech = data.get("speech", "")
        emotion = data.get("emotion", "neutral")
        thoughts = data.get("thoughts", "")
        actions = data.get("actions", [])

        lines = []

        if speech:
            lines.append(f"**NAO :** {speech}")

        non_speech = [a for a in actions if a.get("type") != "speech"]
        if non_speech:
            icons = {"motion": "🤸", "leds": "💡", "move": "🚶", "look": "👀"}
            parts = []
            for a in non_speech:
                icon = icons.get(a.get("type", ""), "•")
                detail = {k: v for k, v in a.items() if k not in ("type", "body")}
                parts.append(
                    f"{icon} `{a.get('type')}` {json.dumps(detail, ensure_ascii=False)}"
                )
            lines.append("\n*Actions :*\n" + "\n".join(parts))

        if thoughts:
            lines.append(f"\n*({emotion}) {thoughts}*")

        return "\n".join(lines) if lines else "Action exécutée."
