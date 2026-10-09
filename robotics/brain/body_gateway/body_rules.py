"""
body_rules.py — Règles de body_gateway sans dépendance ROS 2 (testables seules).

  - type d'un corps d'après son identifiant (nao_01 → nao, static_01 → static)
  - table de routage : quel corps exécute quel type d'action
  - mot de réveil pour les corps qui envoient toute la parole entendue (static)
  - direction de la voix : texte pour le LLM et angle de tête du NAO

Angles du drone statique (ADR 004) : degrés, 0 = axe de la caméra,
positif = à droite.  HeadYaw du NAO : radians, positif = vers la gauche du robot.
"""

import math
import re
import time
import unicodedata

# ─────────────────────────────────────────────────────────────────────────────
# Body types and capabilities
# ─────────────────────────────────────────────────────────────────────────────

# Actions each body type can execute.  The NAO keeps "speech" so that it still
# talks when no static drone is connected.
CAPABILITIES: dict[str, set[str]] = {
    "static": {"speech"},
    "nao":    {"speech", "motion", "leds", "move", "look"},
}

# One line per body type for the LLM system prompt.
CAPABILITY_LABELS: dict[str, str] = {
    "static": "parler, entendre, voir (haut-parleur, micros, caméra de la pièce)",
    "nao":    "gestes, tête, LEDs (motion, look, leds, move)",
}

# Preferred body type for each action type, when that body is connected.
PREFERRED_TYPE: dict[str, str] = {
    "speech": "static",
}

HEAD_YAW_LIMIT_RAD = 2.0857   # cf. motion_engine/nao_joints.py JOINT_LIMITS["HeadYaw"]


def body_type(body_id: str) -> str:
    """'static_01' → 'static', anything else → 'nao' (every body was a NAO
    before ADR 004)."""
    return "static" if body_id.startswith("static") else "nao"


def bodies_of_type(registry: list[str], kind: str) -> list[str]:
    return [b for b in registry if body_type(b) == kind]


def describe_bodies(registry: list[str], speaker: str) -> str:
    """Lines '- id : capacités' injected in the system prompt.

    A NAO that is the speaking body (no static drone connected) also gets "parler".
    """
    lines = []
    for b in registry:
        label = CAPABILITY_LABELS[body_type(b)]
        if b == speaker and body_type(b) != "static":
            label = f"parler, {label}"
        lines.append(f"- {b} : {label}")
    return "\n".join(lines)


def speech_body(registry: list[str], connected: dict[str, bool], default: str) -> str:
    """Body that should speak: first connected static drone, else `default`."""
    for b in bodies_of_type(registry, "static"):
        if connected.get(b, False):
            return b
    return default


def route_action(action: dict, registry: list[str], connected: dict[str, bool]) -> str | None:
    """Return the body that must execute this action, or None if unknown body.

    1. Preferred body type for this action (speech → static) when connected.
    2. The body named by the LLM if it has the capability.
    3. Otherwise the first registered body able to do it (static → NAO for gestures).
    """
    kind = action.get("type", "")
    requested = action.get("body", registry[0] if registry else "")
    if requested not in registry:
        return None

    preferred = PREFERRED_TYPE.get(kind)
    if preferred:
        for b in bodies_of_type(registry, preferred):
            if connected.get(b, False):
                return b

    if kind in CAPABILITIES[body_type(requested)]:
        # A static drone that is offline cannot speak: fall back to a NAO.
        if body_type(requested) == "static" and not connected.get(requested, False):
            for b in registry:
                if body_type(b) != "static" and kind in CAPABILITIES[body_type(b)]:
                    return b
        return requested

    for b in registry:
        if kind in CAPABILITIES[body_type(b)]:
            return b
    return requested


def without_speech(actions: list) -> list:
    """Actions minus "speech": used when the caller plays the voice itself (OBS)."""
    return [a for a in actions if a.get("type") != "speech"]


# ─────────────────────────────────────────────────────────────────────────────
# Wake word
# ─────────────────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Lowercase, no accents, no punctuation, 'okay' → 'ok'."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bokay\b|\bo k\b", "ok", text)
    return " ".join(text.split())


class WakeWordGate:
    """Filter the speech of bodies that transcribe everything they hear.

    The NAO detects "ok nao" on board and only sends what follows.  The static
    drone sends every speech segment, so the same rule is applied here on the
    transcribed text:
      - "ok nao, quelle heure est-il ?" → "quelle heure est-il ?"
      - "ok nao" alone → nothing yet, the next sentence within `window_s`
        seconds is accepted without the wake word
      - anything else → ignored (None)
    """

    def __init__(self, wake_word: str, window_s: float = 10.0):
        self._wake = normalize(wake_word)
        self._window_s = window_s
        self._armed_until: dict[str, float] = {}

    def filter(self, body_id: str, text: str, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        if not self._wake:
            return text
        words = text.split()
        # Find the wake word on normalised words, then cut the original text.
        for i in range(len(words)):
            for j in range(i + 1, min(len(words), i + 6) + 1):
                if normalize(" ".join(words[i:j])) == self._wake:
                    rest = " ".join(words[j:]).lstrip(" ,.;:!?-").strip()
                    if rest:
                        self._armed_until.pop(body_id, None)
                        return rest
                    self._armed_until[body_id] = now + self._window_s
                    return None
        if self.armed(body_id, now):
            self._armed_until.pop(body_id, None)
            return text
        return None

    def armed(self, body_id: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        until = self._armed_until.get(body_id)
        return until is not None and until >= now


# ─────────────────────────────────────────────────────────────────────────────
# Voice direction
# ─────────────────────────────────────────────────────────────────────────────

def direction_label(azimuth_deg: float | None) -> str:
    """'[voix : 30° à gauche]' for the LLM context, '' when unknown."""
    if azimuth_deg is None:
        return ""
    a = round(float(azimuth_deg))
    if abs(a) < 5:
        return "[voix : en face]"
    return f"[voix : {abs(a)}° à {'droite' if a > 0 else 'gauche'}]"


def head_yaw_rad(azimuth_deg: float, offset_deg: float = 0.0) -> float:
    """NAO HeadYaw (rad) to look at a voice heard by the static drone.

    The drone counts positive to the right, NAO HeadYaw positive to the left,
    hence the minus sign.  NAO_HEAD_YAW_OFFSET (degrees, set by hand) accounts
    for the NAO not facing the same way as the camera.  Result clamped to the
    HeadYaw joint limits.
    """
    yaw = math.radians(-float(azimuth_deg) + float(offset_deg))
    return max(-HEAD_YAW_LIMIT_RAD, min(HEAD_YAW_LIMIT_RAD, yaw))
