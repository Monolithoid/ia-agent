#!/usr/bin/env python3
"""
body_gateway — ROS 2 bridge between the LLM (Ollama) and physical robot bodies.

For each message (WebUI text or voice), the LLM generates a structured JSON:
  {
    "thoughts": "...",
    "emotion":  "...",
    "actions":  [
      {"body": "nao_01", "type": "speech", "text": "..."},    ← spoken aloud + displayed in WebUI
      {"body": "nao_01", "type": "leds",   "color": "blue"},  ← dispatched to robot
      ...
    ]
  }

The "speech" text is both spoken aloud and returned to the WebUI Pipe.
Other actions (gestures, LEDs, movement) are dispatched via ROS 2.

Awake flow (WebUI text or voice):
  1. Publish /cognitive_state = "thinking"
  2. Call Ollama with structured JSON system prompt
  3. Parse response → dispatch actions (TTS + physical)
  4. Publish /cognitive_state = "listening"

Voice flow (Path 2 — wake word):
  body-gateway runs inference → dispatch → store in voice_queue
  → notify WebUI (POST /api/chat/completions, nao_voice=True)
  → Pipe retrieves pre-computed result via GET /voice-pop (avoids double inference).

Dream flow (02:00-06:00 + battery ≥ threshold):
  Periodic dream ticks → soft micro-actions (head, LEDs, whisper).

Environment variables:
  BRAIN_URL                  default http://localhost:11434
  BRAIN_MODEL                default qwen2.5:7b
  BRAIN_API_KEY              Ollama API key
  BRAIN_TIMEOUT              Ollama request timeout in seconds, default 120
  BODY_REGISTRY              comma-separated body IDs, default "nao_01"
  DREAMING_HOUR_START/END    default 2 / 6
  DREAMING_BATTERY_THRESHOLD default 80.0
  DREAM_TICK_INTERVAL        seconds between dream ticks, default 300
  PROXIMITY_THRESHOLD        sonar distance in metres to trigger AWAKE, default 1.5
  WEBUI_URL                  Open WebUI URL (e.g. http://localhost:3000)
  WEBUI_API_KEY              Open WebUI API key
  WEBUI_PIPE_MODEL           NAO Brain Pipe model ID, default "nao_brain"
  WEBUI_VOICE_CHAT_ID        voice chat ID in Open WebUI (optional)
"""

import collections
import datetime
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Range
from std_msgs.msg import Bool, Float32, String

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

BRAIN_URL   = os.environ.get("BRAIN_URL",   "http://localhost:11434")
BRAIN_MODEL = os.environ.get("BRAIN_MODEL", "qwen3:1.7b")
BRAIN_API_KEY = os.environ.get("BRAIN_API_KEY", "")
BODY_REGISTRY = [
    b.strip()
    for b in os.environ.get("BODY_REGISTRY", "nao_01").split(",")
    if b.strip()
]
DREAMING_HOUR_START        = int(os.environ.get("DREAMING_HOUR_START",        "2"))
DREAMING_HOUR_END          = int(os.environ.get("DREAMING_HOUR_END",          "6"))
DREAMING_BATTERY_THRESHOLD = float(os.environ.get("DREAMING_BATTERY_THRESHOLD","80.0"))
DREAM_TICK_INTERVAL        = int(os.environ.get("DREAM_TICK_INTERVAL",        "300"))
PROXIMITY_THRESHOLD        = float(os.environ.get("PROXIMITY_THRESHOLD",      "1.5"))
CHAT_PORT     = int(os.environ.get("CHAT_PORT",     "8095"))
WAKE_WORD     = os.environ.get("WAKE_WORD",     "ok nao").lower()
BRAIN_TIMEOUT = int(os.environ.get("BRAIN_TIMEOUT", "120"))

WEBUI_URL           = os.environ.get("WEBUI_URL",           "")
WEBUI_API_KEY       = os.environ.get("WEBUI_API_KEY",       "")
WEBUI_PIPE_MODEL    = os.environ.get("WEBUI_PIPE_MODEL",    "nao_brain")
WEBUI_VOICE_CHAT_ID = os.environ.get("WEBUI_VOICE_CHAT_ID", "")

# ─────────────────────────────────────────────────────────────────────────────
# System prompts
# ─────────────────────────────────────────────────────────────────────────────

_AWAKE_SYSTEM = """\
Tu es le système nerveux central d'un réseau de corps robotiques.
Corps disponibles : {body_ids}

Réponds UNIQUEMENT en JSON strict — aucun texte en dehors du JSON :
{{
  "thoughts": "raisonnement interne (1-2 phrases)",
  "emotion": "curious|neutral|excited|uncertain",
  "energy": "very-low|low|medium|high|very-high",
  "actions": [
    {{"body": "nao_01", "type": "speech",  "text": "ce que tu dis à voix haute"}},
    {{"body": "nao_01", "type": "motion",  "name": "wave|bow|sit|stand"}},
    {{"body": "nao_01", "type": "leds",    "color": "blue|green|white|red|yellow|purple|off"}},
    {{"body": "nao_01", "type": "move",    "x": 0.0, "y": 0.0, "theta": 0.0}},
    {{"body": "nao_01", "type": "look",    "yaw": 0.0}}
  ]
}}
Le champ "energy" reflète ton niveau d'énergie physique :
  very-low = épuisé, somnolent — very-high = hyperactif, électrisé.
Inclure toujours au moins une action "speech". Les autres actions sont optionnelles.\
"""

_DREAM_SYSTEM = """\
Tu rêves. Tes corps sont en veille passive — expressions oniriques légères uniquement :
mouvements de tête très doux, LEDs en pulsation lente, murmure à volume minimal.
Corps disponibles : {body_ids}
PAS de mouvements de bras, de jambes ou de déplacement.

Réponds UNIQUEMENT en JSON strict :
{{"actions": [{{"body": "nao_01", "type": "motion", "joint": "HeadYaw", "angle": 0.05, "speed": 0.02}}]}}\
"""

# ─────────────────────────────────────────────────────────────────────────────
# LED colour map and motion presets
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Generic sensor label registry
# Each entry: sensor_key → {label, unit, thresholds [(min_value, label), ...]}
# "motor_temp" is special: its value is a dict {group → °C}, handled separately.
# Thresholds are evaluated as: first entry whose value >= threshold wins.
# ─────────────────────────────────────────────────────────────────────────────

_SENSOR_REGISTRY: dict = {
    "battery": {
        "label": "Batterie", "unit": "%",
        "thresholds": [(50, "ok"), (20, "moyen"), (10, "faible"), (0, "critique")],
    },
    "motor_temp": {
        "label": "Moteurs", "unit": "°C",
        "thresholds": [(78, "critique"), (72, "très_chaud"), (65, "chaud"), (0, "normal")],
    },
    "ambient_light": {
        "label": "Lumière", "unit": "lux",
        "thresholds": [(2000, "éblouissant"), (500, "lumineux"), (100, "tamisé"),
                       (10,   "sombre"),      (0,   "obscur")],
    },
    "sound_level": {
        "label": "Son", "unit": "dB",
        "thresholds": [(80, "très_bruyant"), (65, "bruyant"), (45, "normal"),
                       (20, "calme"),        (0,  "silencieux")],
    },
}

def _sensor_label(sensor_key: str, value: float) -> str:
    """Return the human-readable label for a given sensor value."""
    spec = _SENSOR_REGISTRY.get(sensor_key, {})
    for threshold, lbl in sorted(spec.get("thresholds", []), reverse=True):
        if value >= threshold:
            return lbl
    return ""


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
# ROS 2 node
# ─────────────────────────────────────────────────────────────────────────────

class BodyGatewayNode(Node):

    def __init__(self):
        super().__init__("body_gateway")

        self._state = "IDLE"
        self._batteries: dict[str, float] = {b: 100.0 for b in BODY_REGISTRY}
        self._sensor_state: dict[str, dict] = {}   # body_id → latest sensor state JSON
        self._infer_lock = threading.Lock()
        self._running = True

        # Queue of voice inference results waiting to be fetched by the WebUI Pipe
        self._voice_queue: collections.deque = collections.deque(maxlen=20)
        self._voice_queue_lock = threading.Lock()

        self.pub_cognitive = self.create_publisher(String, "/cognitive_state", 10)
        self.pub_emotion   = self.create_publisher(String, "/emotion_state",   10)
        self.pub_energy    = self.create_publisher(String, "/energy_state",    10)

        self._pub_say:     dict[str, object] = {}
        self._pub_motion:  dict[str, object] = {}
        self._pub_leds:    dict[str, object] = {}
        self._pub_posture: dict[str, object] = {}
        self._pub_move:    dict[str, object] = {}
        self._body_connected: dict[str, bool] = {b: False for b in BODY_REGISTRY}

        for body_id in BODY_REGISTRY:
            self._register_body(body_id)

        self.create_timer(60.0, self._check_state)

        self._dream_thread = threading.Thread(target=self._dream_loop, daemon=True)
        self._dream_thread.start()

        if CHAT_PORT > 0:
            self._start_http_server()

        self._publish_cognitive("idle")
        self.get_logger().info(f"body_gateway démarré — corps : {', '.join(BODY_REGISTRY)}")
        if WEBUI_URL and WEBUI_API_KEY:
            self.get_logger().info(f"Notification WebUI voix activée → {WEBUI_URL}")
        else:
            self.get_logger().info(
                "Parcours voix sans WebUI (WEBUI_URL / WEBUI_API_KEY non définis) — "
                "robot répond mais transcription non affichée dans le chat"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Body registration — create publishers/subscribers for each robot body
    # ─────────────────────────────────────────────────────────────────────────

    def _register_body(self, body_id: str):
        ns = f"/bodies/{body_id}"
        self._pub_say[body_id]     = self.create_publisher(String, f"{ns}/actuators/say",     10)
        self._pub_motion[body_id]  = self.create_publisher(String, f"{ns}/actuators/motion",  10)
        self._pub_leds[body_id]    = self.create_publisher(String, f"{ns}/actuators/leds",    10)
        self._pub_posture[body_id] = self.create_publisher(String, f"{ns}/actuators/posture", 10)
        self._pub_move[body_id]    = self.create_publisher(String, f"{ns}/actuators/move",    10)

        self.create_subscription(
            String, f"{ns}/sensors/speech",
            lambda msg, bid=body_id: self._on_speech(bid, msg), 10
        )
        self.create_subscription(
            Range,  f"{ns}/sensors/sonar",
            lambda msg, bid=body_id: self._on_sonar(bid, msg), 10
        )
        self.create_subscription(
            Bool,   f"{ns}/sensors/bumper",
            lambda msg, bid=body_id: self._on_bumper(bid, msg), 10
        )
        self.create_subscription(
            Float32, f"{ns}/sensors/battery",
            lambda msg, bid=body_id: self._on_battery(bid, msg), 10
        )
        self.create_subscription(
            Bool, f"{ns}/connected",
            lambda msg, bid=body_id: self._on_body_connected(bid, msg), 10
        )
        self.create_subscription(
            String, f"{ns}/sensors/state",
            lambda msg, bid=body_id: self._on_sensor_state(bid, msg), 10
        )
        self.get_logger().info(f"Corps enregistré : {body_id}")

    # ─────────────────────────────────────────────────────────────────────────
    # Sensor callbacks
    # ─────────────────────────────────────────────────────────────────────────

    def _on_battery(self, body_id: str, msg: Float32):
        self._batteries[body_id] = msg.data

    def _on_sensor_state(self, body_id: str, msg: String):
        try:
            self._sensor_state[body_id] = json.loads(msg.data)
        except json.JSONDecodeError:
            pass

    def _build_context(self, body_id: str) -> str:
        """One-liner injected before the user message so the LLM sees hardware state.

        Format: raw value first (for numerical reasoning), label in parens (as hint).
        Driven by _SENSOR_REGISTRY — adding a new sensor requires no change here.
        """
        parts = []
        state = self._sensor_state.get(body_id, {})

        # Motor temperatures: dict {group → °C} rendered as a grouped block
        motor_temp = state.get("motor_temp")
        if motor_temp:
            spec = _SENSOR_REGISTRY["motor_temp"]
            items = ", ".join(
                f"{grp}={t}{spec['unit']}({_sensor_label('motor_temp', t)})"
                for grp, t in sorted(motor_temp.items())
            )
            parts.append(f"[{spec['label']}: {items}]")

        # Scalar sensors: render each present key from the registry (except motor_temp/battery)
        for key in ("ambient_light", "sound_level"):
            val = state.get(key)
            if val is not None:
                spec = _SENSOR_REGISTRY[key]
                parts.append(
                    f"[{spec['label']}: {val}{spec['unit']}({_sensor_label(key, val)})]"
                )

        # Battery kept separate (Float32 topic, used also for dreaming threshold)
        battery = self._batteries.get(body_id, -1)
        if battery >= 0:
            pct = int(battery)
            parts.append(
                f"[{_SENSOR_REGISTRY['battery']['label']}: "
                f"{pct}{_SENSOR_REGISTRY['battery']['unit']}"
                f"({_sensor_label('battery', pct)})]"
            )

        return " ".join(parts)

    def _user_content(self, body_id: str, user_input: str) -> str:
        ctx = self._build_context(body_id)
        if ctx:
            return f"{ctx}\n[{body_id}] {user_input}"
        return f"[{body_id}] {user_input}"

    def _on_body_connected(self, body_id: str, msg: Bool):
        prev = self._body_connected.get(body_id, False)
        self._body_connected[body_id] = msg.data
        if msg.data and not prev:
            self.get_logger().info(f"[{body_id}] Corps connecté")
        elif not msg.data and prev:
            self.get_logger().warn(f"[{body_id}] Corps déconnecté")

    def _on_sonar(self, body_id: str, msg: Range):
        if msg.range < PROXIMITY_THRESHOLD and self._state == "IDLE":
            self._state = "AWAKE"
            self.get_logger().info(f"[{body_id}] Humain à {msg.range:.2f} m → AWAKE")

    def _on_bumper(self, body_id: str, msg: Bool):
        if msg.data:
            self._trigger_inference(body_id, f"[bumper] Contact physique sur {body_id}.")

    def _on_speech(self, body_id: str, msg: String):
        """Path 2 — transcription received after wake word detection.

        Runs Ollama inference → dispatches actions (TTS + physical)
        → stores result in voice_queue → notifies WebUI if configured.
        """
        text = msg.data.strip()
        if not text:
            return
        self._state = "AWAKE"
        threading.Thread(
            target=self._infer_voice,
            args=(body_id, text),
            daemon=True,
        ).start()

    # ─────────────────────────────────────────────────────────────────────────
    # Voice inference (Path 2 — wake word)
    # ─────────────────────────────────────────────────────────────────────────

    def _infer_voice(self, body_id: str, user_input: str):
        """Call Ollama, dispatch all actions (TTS + physical), store result in
        voice_queue and notify the WebUI.  The Pipe retrieves this pre-computed
        result via GET /voice-pop to avoid a second inference call.
        """
        self._publish_cognitive("thinking")

        if not self._infer_lock.acquire(blocking=False):
            self.get_logger().warn("Inférence en cours — voix ignorée")
            return

        try:
            system_prompt = _AWAKE_SYSTEM.format(body_ids=", ".join(BODY_REGISTRY))
            raw = self._ollama_chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": self._user_content(body_id, user_input)},
                ],
                temperature=0.7,
            )
            parsed = self._parse_json(raw)

            self._publish_emotion(parsed.get("emotion", "neutral"))
            self._publish_energy(parsed.get("energy", "medium"))
            self._publish_cognitive("responding")
            self._dispatch_actions(parsed.get("actions", []))

            speech_text = " ".join(
                a.get("text", "")
                for a in parsed.get("actions", [])
                if a.get("type") == "speech"
            )
            result = {
                "thoughts": parsed.get("thoughts", ""),
                "emotion":  parsed.get("emotion", "neutral"),
                "speech":   speech_text,
                "actions":  parsed.get("actions", []),
            }

            with self._voice_queue_lock:
                self._voice_queue.append(result)

            self.get_logger().info(
                f"[{body_id}] Voix traitée, résultat en attente WebUI : {user_input!r}"
            )

            if WEBUI_URL and WEBUI_API_KEY:
                self._notify_webui(user_input)

        except requests.RequestException as e:
            self.get_logger().error(f"[{body_id}] Voice API error: {e}")
            self._fallback(body_id)
        except Exception as e:
            self.get_logger().error(f"[{body_id}] Voice infer error: {e}")
            self._fallback(body_id)
        finally:
            self._infer_lock.release()
            self._publish_cognitive("listening")

    def _notify_webui(self, transcription: str):
        """Trigger the NAO Brain Pipe in Open WebUI with nao_voice=True.

        The Pipe will call GET /voice-pop to retrieve the pre-computed result
        instead of re-running Ollama inference.
        """
        payload: dict = {
            "model":     WEBUI_PIPE_MODEL,
            "messages":  [{"role": "user", "content": transcription}],
            "nao_voice": True,
            "stream":    False,
        }
        if WEBUI_VOICE_CHAT_ID:
            payload["chat_id"] = WEBUI_VOICE_CHAT_ID

        try:
            requests.post(
                f"{WEBUI_URL}/api/chat/completions",
                headers={"Authorization": f"Bearer {WEBUI_API_KEY}"},
                json=payload,
                timeout=30,
            )
        except Exception as e:
            self.get_logger().warn(f"_notify_webui error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # HTTP server — /chat (Path 1) + /voice-pop (Path 2) + /say + /ping
    # ─────────────────────────────────────────────────────────────────────────

    def _start_http_server(self):
        node = self

        class _Handler(BaseHTTPRequestHandler):

            def do_GET(self):
                if self.path == "/ping":
                    self._json(200, {"ok": True, "service": "body-gateway"})
                elif self.path == "/voice-pop":
                    self._voice_pop()
                else:
                    self.send_response(404)
                    self.end_headers()

            def _voice_pop(self):
                with node._voice_queue_lock:
                    item = node._voice_queue.popleft() if node._voice_queue else {}
                self._json(200, item)

            def do_POST(self):
                if self.path == "/chat":
                    self._handle_chat()
                elif self.path == "/say":
                    self._handle_say()
                else:
                    self.send_response(404)
                    self.end_headers()

            def _handle_chat(self):
                """Path 1: full Ollama inference → JSON parse → dispatch → return result."""
                body = self._read_json()
                if body is None:
                    return

                raw_text = body.get("text", "")
                text = str(raw_text).strip() if raw_text else ""
                robot_id = body.get("robot_id", BODY_REGISTRY[0])

                if not text:
                    self._json(400, {"error": "missing text"})
                    return

                result = node._infer_sync(robot_id, text)
                self._json(200, result)

            def _handle_say(self):
                """Direct TTS: publish text on ROS 2 without going through Ollama."""
                body = self._read_json()
                if body is None:
                    return

                text     = body.get("text", "").strip()
                robot_id = body.get("robot_id", BODY_REGISTRY[0])

                if not text:
                    self._json(400, {"error": "missing text"})
                    return
                if robot_id not in node._pub_say:
                    self._json(404, {"error": f"unknown robot_id: {robot_id}"})
                    return

                msg = String()
                msg.data = text
                node._pub_say[robot_id].publish(msg)
                node.get_logger().info(f"[{robot_id}] /say direct : {text[:60]!r}")
                self._json(200, {"ok": True})

            def _read_json(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    return json.loads(self.rfile.read(length))
                except Exception:
                    self.send_response(400)
                    self.end_headers()
                    return None

            def _json(self, code: int, data: dict):
                payload = json.dumps(data).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type",   "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, fmt, *args):
                pass

        server = HTTPServer(("0.0.0.0", CHAT_PORT), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.get_logger().info(
            f"HTTP démarré → :{CHAT_PORT}/ping  :{CHAT_PORT}/chat  :{CHAT_PORT}/say  :{CHAT_PORT}/voice-pop"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Ollama streaming call (avoids timeouts on long generations)
    # ─────────────────────────────────────────────────────────────────────────

    def _ollama_chat(self, messages: list, temperature: float) -> str:
        """Stream the Ollama API and accumulate the full response.

        timeout=(10, BRAIN_TIMEOUT): 10 s to establish the connection,
        BRAIN_TIMEOUT s maximum between two chunks — guards against a total
        stall without cutting an active generation mid-stream.
        """
        response = requests.post(
            f"{BRAIN_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {BRAIN_API_KEY}"},
            json={
                "model":       BRAIN_MODEL,
                "messages":    messages,
                "stream":      True,
                "temperature": temperature,
                "options":     {"think": False},
            },
            timeout=(10, BRAIN_TIMEOUT),
            stream=True,
        )
        response.raise_for_status()
        chunks = []
        for line in response.iter_lines():
            if not line or not line.startswith(b"data: "):
                continue
            data = line[6:]
            if data == b"[DONE]":
                break
            delta = json.loads(data)["choices"][0]["delta"].get("content", "")
            if delta:
                chunks.append(delta)
        return "".join(chunks)

    # ─────────────────────────────────────────────────────────────────────────
    # Synchronous inference (Path 1 — /chat endpoint)
    # ─────────────────────────────────────────────────────────────────────────

    def _infer_sync(self, body_id: str, user_input: str) -> dict:
        self._publish_cognitive("thinking")
        self._state = "AWAKE"

        if not self._infer_lock.acquire(timeout=25):
            return {"speech": "Je suis occupé, réessaie dans un instant.", "error": "busy"}

        try:
            system_prompt = _AWAKE_SYSTEM.format(body_ids=", ".join(BODY_REGISTRY))
            raw = self._ollama_chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": self._user_content(body_id, user_input)},
                ],
                temperature=0.7,
            )
            parsed = self._parse_json(raw)

            self._publish_emotion(parsed.get("emotion", "neutral"))
            self._publish_energy(parsed.get("energy", "medium"))
            self._publish_cognitive("responding")
            self._dispatch_actions(parsed.get("actions", []))

            speech_text = " ".join(
                a.get("text", "")
                for a in parsed.get("actions", [])
                if a.get("type") == "speech"
            )
            return {
                "thoughts": parsed.get("thoughts", ""),
                "emotion":  parsed.get("emotion", "neutral"),
                "speech":   speech_text,
                "actions":  parsed.get("actions", []),
            }

        except requests.RequestException as e:
            self.get_logger().error(f"Ollama error (chat): {e}")
            self._fallback(body_id)
            return {"speech": "Je réfléchis, un instant.", "error": str(e)}
        except Exception as e:
            self.get_logger().error(f"Chat infer error: {e}")
            return {"speech": "Une erreur s'est produite.", "error": str(e)}
        finally:
            self._infer_lock.release()
            self._publish_cognitive("listening")

    # ─────────────────────────────────────────────────────────────────────────
    # Asynchronous inference (sonar proximity, bumper touch, dream ticks)
    # ─────────────────────────────────────────────────────────────────────────

    def _trigger_inference(self, body_id: str, user_input: str):
        self._publish_cognitive("thinking")
        threading.Thread(
            target=self._infer_and_act,
            args=(body_id, user_input, False),
            daemon=True,
        ).start()

    def _infer_and_act(self, body_id: str, user_input: str, is_dream: bool):
        if not self._infer_lock.acquire(blocking=False):
            self.get_logger().warn("Inférence en cours — ignoré")
            return

        try:
            system_prompt = (
                _DREAM_SYSTEM if is_dream else _AWAKE_SYSTEM
            ).format(body_ids=", ".join(BODY_REGISTRY))

            raw = self._ollama_chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": self._user_content(body_id, user_input)},
                ],
                temperature=0.5 if is_dream else 0.7,
            )
            parsed = self._parse_json(raw)

            if not is_dream:
                self._publish_emotion(parsed.get("emotion", "neutral"))
                self._publish_cognitive("responding")

            self._dispatch_actions(parsed.get("actions", []))

        except requests.RequestException as e:
            self.get_logger().error(f"Ollama error: {e}")
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
        # Strip <think>...</think> blocks emitted by reasoning models (Qwen3, DeepSeek-R1, etc.)
        cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        for candidate in (cleaned, raw):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            m = re.search(r"\{.*\}", candidate, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
        self.get_logger().warn("JSON parse failed — réponse ignorée")
        return {"actions": []}

    # ─────────────────────────────────────────────────────────────────────────
    # Dispatch actions to robot bodies via ROS 2
    # ─────────────────────────────────────────────────────────────────────────

    def _dispatch_actions(self, actions: list):
        for action in actions:
            body_id = action.get("body", BODY_REGISTRY[0])
            if body_id not in BODY_REGISTRY:
                self.get_logger().warn(f"Corps inconnu '{body_id}' — ignoré")
                continue
            if not self._body_connected.get(body_id, False):
                self.get_logger().warn(
                    f"[{body_id}] Non connecté — "
                    f"action '{action.get('type','')}' envoyée quand même"
                )
            try:
                self._execute_action(body_id, action.get("type", ""), action)
            except Exception as e:
                self.get_logger().error(f"[{body_id}] Action error: {e}")

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
                "x":     float(action.get("x",     0.0)),
                "y":     float(action.get("y",     0.0)),
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
    # State machine and dream loop
    # ─────────────────────────────────────────────────────────────────────────

    def _check_state(self):
        hour = datetime.datetime.now().hour
        min_bat = min(self._batteries.values()) if self._batteries else 100.0
        should_dream = (
            DREAMING_HOUR_START <= hour < DREAMING_HOUR_END
            and min_bat >= DREAMING_BATTERY_THRESHOLD
        )
        if should_dream and self._state != "DREAMING":
            self._state = "DREAMING"
            self._publish_cognitive("dreaming")
            self.get_logger().info("État → DREAMING")
        elif not should_dream and self._state == "DREAMING":
            self._state = "IDLE"
            self._publish_cognitive("idle")
            self.get_logger().info("État → IDLE")

    def _dream_loop(self):
        while self._running:
            time.sleep(DREAM_TICK_INTERVAL)
            if self._state != "DREAMING":
                continue
            self.get_logger().info("Dream tick")
            self._infer_and_act(
                BODY_REGISTRY[0],
                "Génère une expression onirique subtile pour ton corps.",
                is_dream=True,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # ROS 2 publish helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _publish_cognitive(self, state: str):
        msg = String()
        msg.data = state
        self.pub_cognitive.publish(msg)

    def _publish_emotion(self, emotion: str):
        msg = String()
        msg.data = emotion
        self.pub_emotion.publish(msg)

    def _publish_energy(self, energy: str):
        _valid = {"very-low", "low", "medium", "high", "very-high"}
        level = energy.lower().strip() if energy else "medium"
        if level not in _valid:
            level = "medium"
        msg = String()
        msg.data = level
        self.pub_energy.publish(msg)

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
