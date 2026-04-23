"""
title: NAO Robot Commands
author: ia-agent
version: 0.1.0
license: MIT
description: >
  Contrôle le robot NAO depuis le chat Open WebUI.
  Mot de réveil : « ok nao, [commande] »

  Exemples :
    ok nao, dis bonjour à tout le monde
    ok nao, fais une révérence
    ok nao, lève-toi
    ok nao, mets les LEDs en bleu
    ok nao, avance un peu
    ok nao, arrête-toi

  Installation dans Open WebUI :
    Workspace → Tools → (+) → coller ce fichier complet
    Puis : Workspace → Models → qwen3:8b → activer l'outil NAO Robot Commands
    Et configurer le system prompt ci-dessous dans le champ System Prompt du modèle.

  System prompt recommandé :
    Tu contrôles le robot NAO via les outils disponibles.
    Quand le message de l'utilisateur commence par « ok nao », analyse
    la commande qui suit et appelle l'outil approprié. Pour tout autre
    message, réponds normalement sans toucher aux outils NAO.

required_open_webui_version: 0.3.0
"""

import requests
from pydantic import BaseModel, Field


# Couleurs LED supportées (FR + EN)
_LED_COLORS = {
    "bleu": (0.0, 0.0, 1.0),
    "blue": (0.0, 0.0, 1.0),
    "rouge": (1.0, 0.0, 0.0),
    "red": (1.0, 0.0, 0.0),
    "vert": (0.0, 1.0, 0.0),
    "green": (0.0, 1.0, 0.0),
    "blanc": (1.0, 1.0, 1.0),
    "white": (1.0, 1.0, 1.0),
    "jaune": (1.0, 1.0, 0.0),
    "yellow": (1.0, 1.0, 0.0),
    "violet": (0.5, 0.0, 1.0),
    "purple": (0.5, 0.0, 1.0),
    "orange": (1.0, 0.5, 0.0),
    "off": (0.0, 0.0, 0.0),
    "éteint": (0.0, 0.0, 0.0),
}


class Tools:
    class Valves(BaseModel):
        naoqi_bridge_url: str = Field(
            default="http://naoqi-bridge:8090",
            description=(
                "URL interne du naoqi-bridge dans le réseau Docker ai-network. "
                "Ne pas modifier sauf si le service change de nom ou de port."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── helper interne ─────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict) -> str:
        try:
            r = requests.post(
                f"{self.valves.naoqi_bridge_url}{path}",
                json=payload,
                timeout=15,
            )
            r.raise_for_status()
            return "ok"
        except requests.exceptions.ConnectionError:
            return "Erreur : naoqi-bridge inaccessible — NAO est-il allumé et connecté ?"
        except requests.exceptions.Timeout:
            return "Erreur : timeout — naoqi-bridge ne répond pas."
        except Exception as e:
            return f"Erreur : {e}"

    # ── outils publics (appelés par Qwen via function calling) ─────────────────

    def nao_say(self, text: str) -> str:
        """
        Make NAO say the given text aloud using its text-to-speech engine.
        Use this whenever the user wants NAO to speak, announce, or read something.
        :param text: The sentence for NAO to say (French or English).
        :return: Confirmation or error message.
        """
        result = self._post("/say", {"text": text})
        if result == "ok":
            return f"NAO dit : « {text} »"
        return result

    def nao_gesture(self, posture: str) -> str:
        """
        Make NAO perform a named posture or gesture.
        Valid posture values:
          Stand, Sit, StandZero, Crouch, LyingBack, LyingBelly,
          wave (friendly wave), bow (greeting bow)
        :param posture: Posture or gesture name (case-sensitive).
        :return: Confirmation or error message.
        """
        result = self._post("/posture", {"posture": posture, "speed": 0.5})
        if result == "ok":
            return f"NAO effectue : {posture}"
        return result

    def nao_leds(self, color: str) -> str:
        """
        Change NAO's eye and body LED color.
        Valid color names (French or English):
          bleu/blue, rouge/red, vert/green, blanc/white,
          jaune/yellow, violet/purple, orange, off/éteint
        :param color: Color name in French or English.
        :return: Confirmation or error message.
        """
        key = color.lower().strip()
        rgb = _LED_COLORS.get(key)
        if rgb is None:
            available = ", ".join(sorted(_LED_COLORS))
            return f"Couleur inconnue : « {color} ». Disponibles : {available}"
        r, g, b = rgb
        result = self._post(
            "/leds",
            {"group": "AllLeds", "r": r, "g": g, "b": b, "duration": 0.5},
        )
        if result == "ok":
            return f"LEDs NAO → {color}"
        return result

    def nao_move(self, x: float = 0.0, y: float = 0.0, theta: float = 0.0) -> str:
        """
        Move NAO continuously until nao_stop is called.
        :param x: Forward (+) / backward (-) velocity, range -1.0 to 1.0.
        :param y: Left (+) / right (-) lateral velocity, range -1.0 to 1.0.
        :param theta: Counter-clockwise (+) / clockwise (-) rotation, range -1.0 to 1.0.
        :return: Confirmation or error message.
        """
        # Clamp values to safe range
        x = max(-1.0, min(1.0, float(x)))
        y = max(-1.0, min(1.0, float(y)))
        theta = max(-1.0, min(1.0, float(theta)))
        result = self._post("/move", {"x": x, "y": y, "theta": theta})
        if result == "ok":
            return f"NAO se déplace (x={x}, y={y}, θ={theta})"
        return result

    def nao_stop(self) -> str:
        """
        Stop NAO's current movement immediately.
        :return: Confirmation or error message.
        """
        try:
            r = requests.post(
                f"{self.valves.naoqi_bridge_url}/move/stop",
                timeout=10,
            )
            r.raise_for_status()
            return "NAO stoppé."
        except requests.exceptions.ConnectionError:
            return "Erreur : naoqi-bridge inaccessible."
        except Exception as e:
            return f"Erreur : {e}"
