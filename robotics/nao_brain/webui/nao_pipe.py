"""
title: NAO Brain
author: ia-agent
version: 0.2.0
description: >
  Connecte le chat Open WebUI au cerveau robotique complet
  (body-gateway → Bitterbot → ROS 2 → NAO / drones / corps enregistrés).

  Le mot de réveil « ok nao » active l'agent. Il répond en langage naturel
  ET pilote simultanément ses corps physiques via le même pipeline qu'en
  interaction directe (parole, sonar, bumper).

  Exemples :
    ok nao, comment tu vas ?
    ok nao, dis bonjour à tout le monde et fais une révérence
    ok nao, avance vers la fenêtre
    ok nao, mets les LEDs en violet et assieds-toi

  Installation dans Open WebUI :
    Workspace → Functions → (+) → coller ce fichier → Type : Pipe → Enregistrer
    Le modèle « NAO Brain » apparaît dans la liste des modèles du chat.

required_open_webui_version: 0.3.0
"""

import json

import requests
from pydantic import BaseModel, Field


class Pipe:
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
        wake_word: str = Field(
            default="ok nao",
            description="Préfixe qui active le pipeline robotique.",
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self) -> list[dict]:
        return [{"id": "nao-brain", "name": "NAO Brain"}]

    def pipe(self, body: dict) -> str:
        messages = body.get("messages", [])
        if not messages:
            return "Aucun message."

        last = messages[-1].get("content", "").strip()
        wake = self.valves.wake_word.lower()

        if not last.lower().startswith(wake):
            return (
                f"Je suis le cerveau de NAO. "
                f"Commence ton message par **« {self.valves.wake_word} »** pour me réveiller."
            )

        command = last[len(wake):].strip().lstrip(",").lstrip(":").strip()
        if not command:
            return "NAO est à l'écoute — que dois-je faire ?"

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
