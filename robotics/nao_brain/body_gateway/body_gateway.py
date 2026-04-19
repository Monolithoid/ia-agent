#!/usr/bin/env python3
"""
body_gateway — Pont ROS2 entre Bitterbot (cerveau) et les corps physiques.

Bitterbot pense et rêve en langage naturel de façon autonome.
Ce gateway intervient uniquement pour forcer une réponse JSON structurée
quand un événement capteur arrive ou lors des micro-actions oniriques.

Flux awake (humain détecté ou parole reçue) :
  1. Publie /cognitive_state = "thinking"  (CognitiveRenderer anime NAO)
  2. Appelle Bitterbot API avec system prompt JSON
  3. Parse la réponse → route les actions vers le bon corps
  4. Publie /cognitive_state = "responding" puis "listening"

Flux dream (02h-06h + batterie ≥ seuil) :
  Toutes les DREAM_TICK_INTERVAL secondes, envoie à Bitterbot un prompt
  onirique → micro-actions douces sur le(s) corps (tête, LEDs, murmure).
  Bitterbot consolide sa mémoire en parallèle via son propre Dream Engine.

Multi-corps :
  BODY_REGISTRY=nao_01,drone_01,nao_02
  Chaque action JSON porte un champ "body" → routage automatique.

Variables d'environnement :
  BITTERBOT_URL              défaut http://localhost:19001
  BITTERBOT_GATEWAY_TOKEN    token d'auth Bitterbot
  BODY_REGISTRY              liste séparée par virgules, défaut "nao_01"
  DREAMING_HOUR_START        défaut 2
  DREAMING_HOUR_END          défaut 6
  DREAMING_BATTERY_THRESHOLD défaut 80.0
  DREAM_TICK_INTERVAL        secondes entre ticks oniriques, défaut 300
  PROXIMITY_THRESHOLD        mètres pour détecter un humain, défaut 1.5
"""

import datetime
import json
import os
import re
import threading
import time

import requests
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Range
from std_msgs.msg import Bool, Float32, String

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

BITTERBOT_URL = os.environ.get("BITTERBOT_URL", "http://localhost:19001")
BITTERBOT_TOKEN = os.environ.get("BITTERBOT_GATEWAY_TOKEN", "")
BODY_REGISTRY = [b.strip() for b in os.environ.get("BODY_REGISTRY", "nao_01").split(",") if b.strip()]
DREAMING_HOUR_START = int(os.environ.get("DREAMING_HOUR_START", "2"))
DREAMING_HOUR_END = int(os.environ.get("DREAMING_HOUR_END", "6"))
DREAMING_BATTERY_THRESHOLD = float(os.environ.get("DREAMING_BATTERY_THRESHOLD", "80.0"))
DREAM_TICK_INTERVAL = int(os.environ.get("DREAM_TICK_INTERVAL", "300"))
PROXIMITY_THRESHOLD = float(os.environ.get("PROXIMITY_THRESHOLD", "1.5"))

# ─────────────────────────────────────────────────────────────────────────────
# Prompts système injectés lors des appels à Bitterbot
# ─────────────────────────────────────────────────────────────────────────────

_AWAKE_SYSTEM = """\
Tu es le système nerveux central d'un réseau de corps robotiques.
Corps disponibles : {body_ids}

Réponds UNIQUEMENT en JSON strict — aucun texte en dehors du JSON :
{{
  "thoughts": "raisonnement interne (1-2 phrases)",
  "emotion": "curious|neutral|excited|uncertain",
  "actions": [
    {{"body": "nao_01", "type": "speech",  "text": "ce que tu dis"}},
    {{"body": "nao_01", "type": "motion",  "name": "wave|bow|sit|stand"}},
    {{"body": "nao_01", "type": "leds",    "color": "blue|green|white|red|yellow|purple|off"}},
    {{"body": "nao_01", "type": "move",    "x": 0.0, "y": 0.0, "theta": 0.0}},
    {{"body": "nao_01", "type": "look",    "yaw": 0.0}}
  ]
}}
N'inclure que les actions nécessaires. Le champ "body" indique quel corps agit.\
"""

_DREAM_SYSTEM = """\
Tu rêves. Tes corps sont en veille passive — tu peux leur envoyer de légères \
expressions oniriques : mouvements de tête très doux, LEDs en pulsation lente, \
murmure à volume minimal si quelque chose doit être dit.
Corps disponibles : {body_ids}
PAS de mouvements de bras, de jambes ou de déplacement.

Réponds UNIQUEMENT en JSON strict :
{{"actions": [{{"body": "nao_01", "type": "motion", "joint": "HeadYaw", "angle": 0.05, "speed": 0.02}}]}}\
"""

# ─────────────────────────────────────────────────────────────────────────────
# Couleurs et presets mouvement
# ─────────────────────────────────────────────────────────────────────────────

_COLORS = {
    "blue":   (0.00, 0.00, 1.00),
    "green":  (0.00, 1.00, 0.00),
    "white":  (1.00, 1.00, 1.00),
    "red":    (1.00, 0.00, 0.00),
    "yellow": (1.00, 0.80, 0.00),
    "purple": (0.50, 0.00, 0.50),
    "off":    (0.00, 0.00, 0.00),
}

_MOTION_PRESETS = {
    "wave": {"joint": "RShoulderPitch", "angle": -1.0, "speed": 0.35},
    "bow":  {"joint": "HeadPitch",      "angle":  0.5, "speed": 0.20},
}


# ─────────────────────────────────────────────────────────────────────────────
# Nœud ROS2
# ─────────────────────────────────────────────────────────────────────────────

class BodyGatewayNode(Node):

    def __init__(self):
        super().__init__("body_gateway")

        self._state = "IDLE"           # IDLE | AWAKE | DREAMING
        self._batteries: dict[str, float] = {b: 100.0 for b in BODY_REGISTRY}
        self._infer_lock = threading.Lock()  # single-flight vers Bitterbot
        self._running = True

        # Publishers cognitifs (pour CognitiveRenderer)
        self.pub_cognitive = self.create_publisher(String, "/cognitive_state", 10)
        self.pub_emotion = self.create_publisher(String, "/emotion_state", 10)

        # Publishers actuateurs et subscribers capteurs, indexés par body_id
        self._pub_say:     dict[str, object] = {}
        self._pub_motion:  dict[str, object] = {}
        self._pub_leds:    dict[str, object] = {}
        self._pub_posture: dict[str, object] = {}
        self._pub_move:    dict[str, object] = {}

        for body_id in BODY_REGISTRY:
            self._register_body(body_id)

        # Timer vérification transition d'état (toutes les 60 s)
        self.create_timer(60.0, self._check_state)

        # Boucle onirique en thread séparé
        self._dream_thread = threading.Thread(target=self._dream_loop, daemon=True)
        self._dream_thread.start()

        self._publish_cognitive("idle")
        self.get_logger().info(
            f"body_gateway démarré — corps : {', '.join(BODY_REGISTRY)}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Enregistrement dynamique d'un corps
    # ─────────────────────────────────────────────────────────────────────────

    def _register_body(self, body_id: str):
        ns = f"/bodies/{body_id}"

        # Actuateurs (→ corps)
        self._pub_say[body_id]     = self.create_publisher(String, f"{ns}/actuators/say", 10)
        self._pub_motion[body_id]  = self.create_publisher(String, f"{ns}/actuators/motion", 10)
        self._pub_leds[body_id]    = self.create_publisher(String, f"{ns}/actuators/leds", 10)
        self._pub_posture[body_id] = self.create_publisher(String, f"{ns}/actuators/posture", 10)
        self._pub_move[body_id]    = self.create_publisher(String, f"{ns}/actuators/move", 10)

        # Capteurs (corps →)
        self.create_subscription(
            String, f"{ns}/sensors/speech",
            lambda msg, bid=body_id: self._on_speech(bid, msg), 10
        )
        self.create_subscription(
            Range, f"{ns}/sensors/sonar",
            lambda msg, bid=body_id: self._on_sonar(bid, msg), 10
        )
        self.create_subscription(
            Bool, f"{ns}/sensors/bumper",
            lambda msg, bid=body_id: self._on_bumper(bid, msg), 10
        )
        self.create_subscription(
            Float32, f"{ns}/sensors/battery",
            lambda msg, bid=body_id: self._on_battery(bid, msg), 10
        )

        self.get_logger().info(f"Corps enregistré : {body_id}")

    # ─────────────────────────────────────────────────────────────────────────
    # Callbacks capteurs
    # ─────────────────────────────────────────────────────────────────────────

    def _on_battery(self, body_id: str, msg: Float32):
        self._batteries[body_id] = msg.data

    def _on_sonar(self, body_id: str, msg: Range):
        if msg.range < PROXIMITY_THRESHOLD and self._state == "IDLE":
            self._state = "AWAKE"
            self.get_logger().info(f"[{body_id}] Humain détecté à {msg.range:.2f} m → AWAKE")

    def _on_bumper(self, body_id: str, msg: Bool):
        if msg.data:
            self._trigger_inference(body_id, f"[bumper] Contact physique sur {body_id}.")

    def _on_speech(self, body_id: str, msg: String):
        text = msg.data.strip()
        if not text:
            return
        self._state = "AWAKE"
        self._trigger_inference(body_id, text)

    # ─────────────────────────────────────────────────────────────────────────
    # Pipeline d'inférence principal
    # ─────────────────────────────────────────────────────────────────────────

    def _trigger_inference(self, body_id: str, user_input: str):
        """Lance l'inférence en arrière-plan ; publie 'thinking' immédiatement."""
        self._publish_cognitive("thinking")
        threading.Thread(
            target=self._infer_and_act,
            args=(body_id, user_input, False),
            daemon=True,
        ).start()

    def _infer_and_act(self, body_id: str, user_input: str, is_dream: bool):
        if not self._infer_lock.acquire(blocking=False):
            self.get_logger().warn("Inférence déjà en cours — événement ignoré")
            return

        try:
            system_prompt = (
                _DREAM_SYSTEM if is_dream else _AWAKE_SYSTEM
            ).format(body_ids=", ".join(BODY_REGISTRY))

            response = requests.post(
                f"{BITTERBOT_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {BITTERBOT_TOKEN}"},
                json={
                    "model": "bitterbot",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": f"[{body_id}] {user_input}"},
                    ],
                    "stream": False,
                    "temperature": 0.5 if is_dream else 0.7,
                },
                timeout=20,
            )
            response.raise_for_status()

            raw = response.json()["choices"][0]["message"]["content"]
            parsed = self._parse_json(raw)

            if not is_dream:
                self._publish_emotion(parsed.get("emotion", "neutral"))
                self._publish_cognitive("responding")

            self._dispatch_actions(parsed.get("actions", []))

        except requests.RequestException as e:
            self.get_logger().error(f"Bitterbot API error: {e}")
            if not is_dream:
                self._fallback(body_id)
        except Exception as e:
            self.get_logger().error(f"Inférence error: {e}")
            if not is_dream:
                self._fallback(body_id)
        finally:
            self._infer_lock.release()
            if not is_dream:
                self._publish_cognitive("listening")

    def _parse_json(self, raw: str) -> dict:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        self.get_logger().warn("JSON parse failed — réponse ignorée")
        return {"actions": []}

    # ─────────────────────────────────────────────────────────────────────────
    # Routage des actions vers les corps
    # ─────────────────────────────────────────────────────────────────────────

    def _dispatch_actions(self, actions: list):
        for action in actions:
            body_id = action.get("body", BODY_REGISTRY[0])
            if body_id not in BODY_REGISTRY:
                self.get_logger().warn(f"Corps inconnu '{body_id}' — action ignorée")
                continue

            kind = action.get("type", "")
            try:
                self._execute_action(body_id, kind, action)
            except Exception as e:
                self.get_logger().error(f"[{body_id}] Action '{kind}' error: {e}")

    def _execute_action(self, body_id: str, kind: str, action: dict):
        if kind == "speech":
            msg = String()
            msg.data = action.get("text", "")
            self._pub_say[body_id].publish(msg)
            time.sleep(0.3)

        elif kind == "motion":
            name = action.get("name", "")
            if name in ("sit", "stand"):
                msg = String()
                msg.data = "Sit" if name == "sit" else "Stand"
                self._pub_posture[body_id].publish(msg)
            elif name in _MOTION_PRESETS:
                msg = String()
                msg.data = json.dumps(_MOTION_PRESETS[name])
                self._pub_motion[body_id].publish(msg)
            elif "joint" in action:
                # Accès direct joint (utilisé dans les prompts oniriques)
                msg = String()
                msg.data = json.dumps({
                    "joint": action["joint"],
                    "angle": float(action.get("angle", 0.0)),
                    "speed": float(action.get("speed", 0.05)),
                })
                self._pub_motion[body_id].publish(msg)

        elif kind == "leds":
            color_name = action.get("color", "white")
            r, g, b = _COLORS.get(color_name, _COLORS["white"])
            msg = String()
            msg.data = json.dumps({"group": "AllLeds", "r": r, "g": g, "b": b, "duration": 0.5})
            self._pub_leds[body_id].publish(msg)

        elif kind == "move":
            msg = String()
            msg.data = json.dumps({
                "x": float(action.get("x", 0.0)),
                "y": float(action.get("y", 0.0)),
                "theta": float(action.get("theta", 0.0)),
            })
            self._pub_move[body_id].publish(msg)

        elif kind == "look":
            msg = String()
            msg.data = json.dumps({
                "joint": "HeadYaw",
                "angle": float(action.get("yaw", 0.0)) * 0.5,
                "speed": 0.12,
            })
            self._pub_motion[body_id].publish(msg)

    def _fallback(self, body_id: str):
        self._publish_cognitive("uncertain")
        msg = String()
        msg.data = "Je réfléchis, un instant s'il vous plaît."
        if body_id in self._pub_say:
            self._pub_say[body_id].publish(msg)
        time.sleep(2.0)

    # ─────────────────────────────────────────────────────────────────────────
    # Machine à états et boucle onirique
    # ─────────────────────────────────────────────────────────────────────────

    def _check_state(self):
        hour = datetime.datetime.now().hour
        # Tous les corps doivent avoir batterie suffisante pour entrer en rêve
        min_battery = min(self._batteries.values()) if self._batteries else 100.0
        should_dream = (
            DREAMING_HOUR_START <= hour < DREAMING_HOUR_END
            and min_battery >= DREAMING_BATTERY_THRESHOLD
        )

        if should_dream and self._state != "DREAMING":
            self._state = "DREAMING"
            self._publish_cognitive("dreaming")
            self.get_logger().info("État → DREAMING")
        elif not should_dream and self._state == "DREAMING":
            self._state = "IDLE"
            self._publish_cognitive("idle")
            self.get_logger().info("État → IDLE (fin du rêve)")

    def _dream_loop(self):
        """Envoie des ticks oniriques à Bitterbot pendant DREAMING."""
        while self._running:
            time.sleep(DREAM_TICK_INTERVAL)
            if self._state != "DREAMING":
                continue
            # Corps principal pour le tick onirique (premier corps enregistré)
            body_id = BODY_REGISTRY[0]
            self.get_logger().info("Dream tick → Bitterbot")
            self._infer_and_act(
                body_id,
                "Génère une expression onirique subtile pour ton corps.",
                is_dream=True,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers publication
    # ─────────────────────────────────────────────────────────────────────────

    def _publish_cognitive(self, state: str):
        msg = String()
        msg.data = state
        self.pub_cognitive.publish(msg)

    def _publish_emotion(self, emotion: str):
        msg = String()
        msg.data = emotion
        self.pub_emotion.publish(msg)

    def destroy_node(self):
        self._running = False
        super().destroy_node()


def main():
    rclpy.init()
    node = BodyGatewayNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
