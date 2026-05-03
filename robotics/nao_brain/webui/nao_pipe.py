"""
title: NAO Brain
author: ia-agent
version: 0.5.0
description: >
  Pipe cerveau — connecte Open WebUI directement au cerveau robotique
  (body-gateway → ROS 2 → NAO / drones / corps enregistrés).

  Chaque message est transmis directement au pipeline cognitif sans
  passer par une inférence LLM intermédiaire. NAO réagit physiquement
  (parole, gestes, LEDs, déplacement) à chaque réponse.

  Parcours voix : quand body-gateway notifie WebUI d'une interaction
  vocale ("Ok nao"), ce Pipe récupère la transcription et la réponse
  pré-calculée depuis /voice-pop et les affiche dans le chat.

  Installation dans Open WebUI :
    Workspace → Functions → (+) → coller ce fichier → Enregistrer
    Puis sélectionner le modèle "NAO Brain" dans le chat.

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

        user_message = messages[-1].get("content", "")
        if not user_message:
            return "Message vide."

        # Parcours 2 : notification vocale émise par body-gateway
        if body.get("nao_voice"):
            return self._handle_voice(user_message)

        # Parcours 1 : message texte de l'utilisateur
        return self._call_gateway(user_message)

    # ─────────────────────────────────────────────────────────────────────────
    # Parcours 2 — récupération de la réponse pré-calculée (voice-pop)
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
                    header = f"*🎤 Transcription : « {transcription} »*\n\n"
                    return header + self._format(data)
        except Exception:
            pass
        # Repli : traiter comme un message texte normal
        return self._call_gateway(transcription)

    # ─────────────────────────────────────────────────────────────────────────
    # Parcours 1 — appel body-gateway
    # ─────────────────────────────────────────────────────────────────────────

    def _call_gateway(self, text: str) -> str:
        try:
            r = requests.post(
                f"{self.valves.body_gateway_url}/chat",
                json={"text": text, "robot_id": self.valves.robot_id},
                timeout=60,
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
