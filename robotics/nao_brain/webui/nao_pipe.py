"""
title: NAO Brain
author: ia-agent
version: 0.7.0
description: >
  Brain Pipe — relays each WebUI message to body-gateway, which calls
  the LLM with a structured JSON prompt.  The response contains:
    • speech  : text that NAO speaks aloud AND displays in the chat
    • actions : gestures, LEDs, movement dispatched to the robot

  Path 1 (text input):
    User types → Pipe → body-gateway /chat → Ollama → JSON
    → robot speaks + acts → WebUI shows "NAO: [speech] + Actions"

  Path 2 (voice "Ok nao"):
    NAO transcribes → body-gateway inference → robot speaks + acts
    → body-gateway stores in voice-queue → notifies WebUI
    → this Pipe retrieves pre-computed result via /voice-pop
    → WebUI displays transcription + response

  Installation in Open WebUI:
    Settings → Admin Settings → Functions → (+) → paste this file → Save
    Then select "NAO Brain" as the model in the chat.

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
                "URL of chat-relay (Caddy proxy on ai-network → body-gateway:8095 on the host)."
            ),
        )
        robot_id: str = Field(
            default="nao_01",
            description="Default target body (must be in BODY_REGISTRY).",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.type = "pipe"
        self.id = "nao_brain"
        self.name = "NAO Brain"

    def pipe(self, body: dict, __user__: dict = {}) -> str:
        """Main Open WebUI entry point called for every chat message.

        Dispatches to the appropriate handler based on message type:
        - __ping__  → connectivity check without Ollama inference
        - nao_voice → retrieve pre-computed voice result (Path 2)
        - default   → full text inference via body-gateway (Path 1)
        """
        messages = body.get("messages", [])
        # Guard: no messages in body — nothing to process.
        if not messages:
            return "Aucun message."

        # Take the last user message (Open WebUI may append an empty
        # assistant entry at the end of the messages array when calling the pipe)
        user_msg = next(
            (m for m in reversed(messages) if m.get("role") == "user"),
            None,
        )
        # Guard: no user-role message found in the list.
        if not user_msg:
            return "Aucun message utilisateur."

        content = user_msg.get("content", "")
        # Open WebUI ≥ 0.4 may send content as a list of content blocks
        # instead of a plain string — flatten to text in that case.
        if isinstance(content, list):
            user_message = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            user_message = content

        # Guard: empty message text — nothing useful to send to the LLM.
        if not user_message:
            return "Message vide."

        # Reserved connectivity test message — no Ollama inference
        if user_message.strip() == "__ping__":
            return self._ping()

        # Path 2 — body-gateway already ran inference on a voice input
        # and stored the result in its voice-queue; retrieve it here.
        if body.get("nao_voice"):
            return self._handle_voice(user_message)

        # Path 1 — regular WebUI text message → full LLM inference.
        return self._call_gateway(user_message)

    # ─────────────────────────────────────────────────────────────────────────
    # Ping — connectivity check pipe → body-gateway (no Ollama call)
    # ─────────────────────────────────────────────────────────────────────────

    def _ping(self) -> str:
        """Check that the pipe can reach body-gateway without triggering inference.

        Returns a user-visible status message indicating whether the robot
        stack is reachable.
        """
        try:
            r = requests.get(
                f"{self.valves.body_gateway_url}/ping",
                timeout=5,
            )
            # Gateway replied and confirmed it is healthy.
            if r.ok and r.json().get("ok"):
                return "✅ body-gateway joignable — pipe opérationnel."
            return f"⚠️ body-gateway a répondu {r.status_code}."
        except requests.exceptions.ConnectionError:
            return (
                "❌ body-gateway inaccessible — "
                "le stack robotique (`docker compose up -d`) est-il démarré ?"
            )
        except Exception as e:
            return f"❌ Erreur ping : {e}"

    # ─────────────────────────────────────────────────────────────────────────
    # Path 2 — retrieve pre-computed result (avoids double inference)
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_voice(self, transcription: str) -> str:
        """Retrieve the pre-computed voice result and display it in the chat.

        body-gateway ran inference immediately on the wake-word audio.
        The Pipe only needs to pop the result from the voice-queue and format
        it — no second Ollama call required.

        Falls back to a normal /chat call if the queue is empty (e.g. the
        voice result expired before the Pipe was notified).
        """
        try:
            r = requests.get(
                f"{self.valves.body_gateway_url}/voice-pop",
                timeout=5,
            )
            if r.ok:
                data = r.json()
                # Non-empty dict means a pre-computed result is available.
                if data:
                    return f"*🎤 « {transcription} »*\n\n" + self._format(data)
        except Exception:
            pass
        # Fallback: treat transcription as a regular text message
        return self._call_gateway(transcription)

    # ─────────────────────────────────────────────────────────────────────────
    # Path 1 — call body-gateway /chat → Ollama (structured JSON)
    # ─────────────────────────────────────────────────────────────────────────

    def _call_gateway(self, text: str) -> str:
        """Forward a text message to body-gateway and return the formatted reply.

        Uses a (connect_timeout=10s, read_timeout=None) pair so the HTTP
        connection is established quickly while allowing long LLM generations.
        """
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
    # Format body-gateway response for display in WebUI
    # ─────────────────────────────────────────────────────────────────────────

    def _format(self, data: dict) -> str:
        """Render the body-gateway JSON response as a human-readable chat message.

        Layout:
          **NAO :** <speech>
          *Actions:* <emoji> type {detail} ...
          *(emotion) thoughts*
        """
        speech = data.get("speech", "")
        emotion = data.get("emotion", "neutral")
        thoughts = data.get("thoughts", "")
        actions = data.get("actions", [])

        lines = []

        # Primary content: what NAO said aloud.
        if speech:
            lines.append(f"**NAO :** {speech}")

        # Secondary content: physical actions (exclude speech, already shown above).
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

        # Tertiary content: internal thoughts shown in italics.
        if thoughts:
            lines.append(f"\n*({emotion}) {thoughts}*")

        return "\n".join(lines) if lines else "Action exécutée."
