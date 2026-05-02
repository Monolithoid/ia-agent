"""
title: NAO Brain
author: ia-agent
version: 0.4.0
description: >
  Console cerveau — connecte Open WebUI directement au cerveau robotique
  (body-gateway → Bitterbot → ROS 2 → NAO / drones / corps enregistrés).

  Chaque message est injecté dans le pipeline cognitif de Bitterbot.
  NAO réagit physiquement (parole, gestes, LEDs, déplacement) à chaque
  réponse du cerveau. Le WebUI sert de console de sondage : ce que vous
  tapez est traité comme une stimulation directe du cerveau.

  Exemples :
    Comment tu vas ?
    Dis bonjour et fais une révérence
    Avance vers la fenêtre
    Mets les LEDs en violet et assieds-toi

  Installation dans Open WebUI :
    Workspace → Tools → (+) → coller ce fichier → Enregistrer
    Puis : Workspace → Models → qwen2.5:7b → activer NAO Brain
    Et coller le system prompt indiqué ci-dessous.

  System prompt recommandé pour qwen2.5:7b :
    Tu es une interface de sondage direct vers le cerveau du robot NAO.
    Pour CHAQUE message de l'utilisateur, sans exception, appelle
    l'outil talk_to_nao en passant le message tel quel comme commande.
    Ne réponds jamais toi-même — laisse toujours le cerveau répondre.

required_open_webui_version: 0.3.0
"""

import json

import requests
from pydantic import BaseModel, Field


class Tools:
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

    def talk_to_nao(self, command: str) -> str:
        """
        Inject a message into the NAO robot brain (Bitterbot via body-gateway).
        The brain processes the message, speaks the response through NAO's speakers,
        and triggers physical reactions: gestures, LEDs, movement.
        Always call this tool for every user message — the brain always responds.
        :param command: The message to send to the robot brain.
        :return: What NAO said and the actions it performed.
        """
        try:
            r = requests.post(
                f"{self.valves.body_gateway_url}/chat",
                json={"text": command, "robot_id": self.valves.robot_id},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.ConnectionError:
            return (
                "❌ body-gateway inaccessible — "
                "le stack robotique (`docker compose --profile robotics up`) est-il démarré ?"
            )
        except Exception as e:
            return f"❌ Erreur : {e}"

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
