"""
title: NAO Brain
author: ia-agent
version: 0.3.0
description: >
  Connecte le chat Open WebUI au cerveau robotique complet
  (body-gateway → Bitterbot → ROS 2 → NAO / drones / corps enregistrés).

  Le mot de réveil « ok nao » active l'agent. Qwen détecte le préfixe
  et appelle cet outil, qui injecte le message dans le même pipeline
  que la parole directe à NAO.

  Exemples :
    ok nao, comment tu vas ?
    ok nao, dis bonjour et fais une révérence
    ok nao, avance vers la fenêtre
    ok nao, mets les LEDs en violet et assieds-toi

  Installation dans Open WebUI :
    Workspace → Tools → (+) → coller ce fichier → Enregistrer
    Puis : Workspace → Models → qwen2.5:7b → activer NAO Brain
    Et coller le system prompt indiqué ci-dessous.

  System prompt recommandé pour qwen2.5:7b :
    Quand le message de l'utilisateur commence par « ok nao »,
    appelle l'outil talk_to_nao avec la commande qui suit le préfixe.
    Pour tout autre message, réponds normalement sans utiliser l'outil.

required_open_webui_version: 0.3.0
"""

import json

import requests
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        body_gateway_url: str = Field(
            default="http://host-gateway:8095",
            description=(
                "URL du body-gateway. host-gateway résout vers l'IP de l'hôte Docker "
                "(configuré via extra_hosts dans open-webui)."
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
        Send a command to the NAO robot brain (Bitterbot via body-gateway).
        The brain processes the command, speaks through NAO, triggers gestures,
        LEDs, movement and any other registered body (drones, etc.).
        Use this tool whenever the user says "ok nao" followed by a command.
        :param command: The instruction to send to the robot brain (without the "ok nao" prefix).
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
