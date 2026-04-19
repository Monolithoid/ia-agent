#!/usr/bin/env python3
"""
NAOBrainAgent — ROS2 Humble node: the cognitive brain of the NAO robot.

State machine:
  AWAKE    — human detected or speech received; infers with Qwen3 (< 5 s target)
  IDLE     — passive scanning, animations, environment awareness
  DREAMING — 02:00–06:00 AND battery ≥ 80 %; hands off to DreamEngine

During every Qwen3 inference, /cognitive_state = "thinking" is published
immediately so CognitiveRenderer keeps NAO visibly alive.

Qwen3 response format (strict JSON only):
{
  "thoughts": "internal reasoning",
  "emotion":  "curious|neutral|excited|uncertain",
  "actions": [
    {"type": "speech",  "text": "..."},
    {"type": "motion",  "name": "wave|bow|sit|stand"},
    {"type": "leds",    "color": "blue|green|white|red|yellow|purple|off"},
    {"type": "move",    "x": 0.0, "y": 0.0, "theta": 0.0},
    {"type": "look",    "yaw": 0.0}
  ]
}

Environment variables:
  OLLAMA_URL                 default http://localhost:11434
  CHROMA_URL                 default http://localhost:8000   (host:port)
  DREAMING_HOUR_START        default 2
  DREAMING_HOUR_END          default 6
  DREAMING_BATTERY_THRESHOLD default 80.0
  MORNING_STATE_ENABLED      default 1   (load morning_state on boot)
"""

import datetime
import json
import os
import re
import threading
import time

import chromadb
import requests
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Range
from std_msgs.msg import Bool, Float32, String

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
CHROMA_HOST = os.environ.get("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
DREAMING_HOUR_START = int(os.environ.get("DREAMING_HOUR_START", "2"))
DREAMING_HOUR_END = int(os.environ.get("DREAMING_HOUR_END", "6"))
DREAMING_BATTERY_THRESHOLD = float(os.environ.get("DREAMING_BATTERY_THRESHOLD", "80.0"))
MORNING_STATE_ENABLED = os.environ.get("MORNING_STATE_ENABLED", "1") == "1"
IDLE_TO_AWAKE_SONAR_THRESHOLD = 1.5  # metres — human detected

SYSTEM_PROMPT = (
    "Tu es le cerveau cognitif d'un robot NAO V4. Tu perçois le monde via des capteurs "
    "et réponds par des actions physiques. Tu es incarné, curieux, expressif.\n"
    "Réponds UNIQUEMENT en JSON strict, sans texte en dehors du JSON:\n"
    '{\n'
    '  "thoughts": "raisonnement interne (1-2 phrases)",\n'
    '  "emotion": "curious|neutral|excited|uncertain",\n'
    '  "actions": [\n'
    '    {"type": "speech",  "text": "ce que tu dis"},\n'
    '    {"type": "motion",  "name": "wave|bow|sit|stand"},\n'
    '    {"type": "leds",    "color": "blue|green|white|red|yellow|purple|off"},\n'
    '    {"type": "move",    "x": 0.0, "y": 0.0, "theta": 0.0},\n'
    '    {"type": "look",    "yaw": 0.0}\n'
    '  ]\n'
    '}\n'
    "N'inclure que les actions nécessaires. Sois naturel et incarné."
)

_COLOR_MAP = {
    "blue":   {"r": 0.00, "g": 0.00, "b": 1.00},
    "green":  {"r": 0.00, "g": 1.00, "b": 0.00},
    "white":  {"r": 1.00, "g": 1.00, "b": 1.00},
    "red":    {"r": 1.00, "g": 0.00, "b": 0.00},
    "yellow": {"r": 1.00, "g": 0.80, "b": 0.00},
    "purple": {"r": 0.50, "g": 0.00, "b": 0.50},
    "off":    {"r": 0.00, "g": 0.00, "b": 0.00},
}

_MOTION_PRESETS = {
    "wave": {"joint": "RShoulderPitch", "angle": -1.0, "speed": 0.35},
    "bow":  {"joint": "HeadPitch",      "angle":  0.5, "speed": 0.20},
}


# ──────────────────────────────────────────────────────────────────────────────
# Agent node
# ──────────────────────────────────────────────────────────────────────────────

class NAOBrainAgent(Node):

    def __init__(self):
        super().__init__("nao_brain_agent")

        self._state = "IDLE"     # IDLE | AWAKE | DREAMING
        self._battery = 100.0
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()  # single-flight inference

        self._context: list[dict] = []       # sliding conversation window (≤ 20 msgs)

        # ── ChromaDB ───────────────────────────────────────────────────────────
        self._chroma = self._init_chroma()

        # ── Publishers ─────────────────────────────────────────────────────────
        self.pub_cognitive = self.create_publisher(String, "/cognitive_state", 10)
        self.pub_emotion = self.create_publisher(String, "/emotion_state", 10)
        self.pub_say = self.create_publisher(String, "/naoqi/say", 10)
        self.pub_motion = self.create_publisher(String, "/naoqi/motion", 10)
        self.pub_leds = self.create_publisher(String, "/naoqi/leds", 10)
        self.pub_posture = self.create_publisher(String, "/naoqi/posture", 10)
        self.pub_move = self.create_publisher(String, "/naoqi/move", 10)

        # ── Subscribers ────────────────────────────────────────────────────────
        self.create_subscription(String, "/naoqi/speech_recognized", self._on_speech, 10)
        self.create_subscription(Float32, "/naoqi/battery", self._on_battery, 10)
        self.create_subscription(Range, "/naoqi/sonar", self._on_sonar, 10)
        self.create_subscription(Bool, "/naoqi/bumper", self._on_bumper, 10)

        # ── State-transition timer (every 60 s) ────────────────────────────────
        self.create_timer(60.0, self._check_state_transition)

        # Load morning_state from previous dream cycle
        if MORNING_STATE_ENABLED:
            self._load_morning_state()

        self._publish_cognitive("idle")
        self.get_logger().info("NAOBrainAgent started")

    # ──────────────────────────────────────────────────────────────────────────
    # ChromaDB initialisation
    # ──────────────────────────────────────────────────────────────────────────

    def _init_chroma(self):
        try:
            client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
            for name in ("episodes_raw", "long_term", "proto_actions",
                         "morning_state", "interaction_ctx"):
                client.get_or_create_collection(name)
            self.get_logger().info("ChromaDB connected")
            return client
        except Exception as e:
            self.get_logger().warn(f"ChromaDB unavailable: {e} — memory disabled")
            return None

    def _load_morning_state(self):
        if not self._chroma:
            return
        try:
            col = self._chroma.get_collection("morning_state")
            result = col.get()
            if result["documents"]:
                morning = json.loads(result["documents"][0])
                mood = morning.get("mood", "neutral")
                self.get_logger().info(f"Morning state loaded — mood: {mood}")
                # Pre-load context with morning intent
                topics = morning.get("priority_topics", [])
                if topics:
                    hint = f"[Contexte du matin: thèmes prioritaires = {', '.join(topics)}]"
                    self._context.append({"role": "system", "content": hint})
        except Exception as e:
            self.get_logger().warn(f"Morning state load error: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Sensor callbacks
    # ──────────────────────────────────────────────────────────────────────────

    def _on_battery(self, msg: Float32):
        self._battery = msg.data

    def _on_sonar(self, msg: Range):
        if msg.range < IDLE_TO_AWAKE_SONAR_THRESHOLD:
            with self._lock:
                if self._state == "IDLE":
                    self._state = "AWAKE"
                    self.get_logger().info("Sonar: human detected — AWAKE")

    def _on_bumper(self, msg: Bool):
        if msg.data:
            self._handle_input("Quelqu'un m'a touché le pied.")

    def _on_speech(self, msg: String):
        text = msg.data.strip()
        if not text:
            return
        with self._lock:
            self._state = "AWAKE"
        self._handle_input(text)

    # ──────────────────────────────────────────────────────────────────────────
    # Core inference pipeline
    # ──────────────────────────────────────────────────────────────────────────

    def _handle_input(self, user_input: str):
        self._publish_cognitive("thinking")
        thread = threading.Thread(
            target=self._infer_and_act, args=(user_input,), daemon=True
        )
        thread.start()

    def _infer_and_act(self, user_input: str):
        # Prevent parallel inferences that would confuse context
        if not self._infer_lock.acquire(blocking=False):
            self.get_logger().warn("Inference already in progress — input dropped")
            return

        try:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages.extend(self._context[-10:])
            messages.append({"role": "user", "content": user_input})

            response = requests.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": "qwen3:8b",
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.7, "num_predict": 512},
                },
                timeout=20,
            )
            response.raise_for_status()

            raw = response.json()["message"]["content"]
            parsed = self._parse_json(raw)

            # Update sliding context window
            self._context.append({"role": "user", "content": user_input})
            self._context.append({"role": "assistant", "content": raw})
            if len(self._context) > 20:
                self._context = self._context[-20:]

            self._store_episode(user_input, parsed)

            # Publish emotion so CognitiveRenderer can prepare
            self._publish_emotion(parsed.get("emotion", "neutral"))

            self._publish_cognitive("responding")
            self._execute_actions(parsed.get("actions", []))

        except requests.RequestException as e:
            self.get_logger().error(f"Ollama request error: {e}")
            self._fallback()
        except Exception as e:
            self.get_logger().error(f"Inference error: {e}")
            self._fallback()
        finally:
            self._infer_lock.release()
            self._publish_cognitive("listening")

    def _parse_json(self, raw: str) -> dict:
        """Defensive JSON parser — handles text surrounding the JSON object."""
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

        self.get_logger().warn("JSON parse failed — using fallback response")
        return {
            "thoughts": "parse error",
            "emotion": "uncertain",
            "actions": [{"type": "speech",
                         "text": "Je n'ai pas bien compris, pouvez-vous répéter ?"}],
        }

    def _execute_actions(self, actions: list):
        for action in actions:
            kind = action.get("type", "")
            try:
                if kind == "speech":
                    msg = String()
                    msg.data = action.get("text", "")
                    self.pub_say.publish(msg)
                    time.sleep(0.4)

                elif kind == "motion":
                    name = action.get("name", "")
                    if name in ("sit", "stand"):
                        msg = String()
                        msg.data = "Sit" if name == "sit" else "Stand"
                        self.pub_posture.publish(msg)
                    elif name in _MOTION_PRESETS:
                        msg = String()
                        msg.data = json.dumps(_MOTION_PRESETS[name])
                        self.pub_motion.publish(msg)

                elif kind == "leds":
                    color_name = action.get("color", "white")
                    c = _COLOR_MAP.get(color_name, _COLOR_MAP["white"])
                    msg = String()
                    msg.data = json.dumps(
                        {"group": "AllLeds", "r": c["r"], "g": c["g"],
                         "b": c["b"], "duration": 0.5}
                    )
                    self.pub_leds.publish(msg)

                elif kind == "move":
                    msg = String()
                    msg.data = json.dumps({
                        "x": float(action.get("x", 0.0)),
                        "y": float(action.get("y", 0.0)),
                        "theta": float(action.get("theta", 0.0)),
                    })
                    self.pub_move.publish(msg)

                elif kind == "look":
                    msg = String()
                    msg.data = json.dumps(
                        {"joint": "HeadYaw",
                         "angle": float(action.get("yaw", 0.0)) * 0.5,
                         "speed": 0.12}
                    )
                    self.pub_motion.publish(msg)

            except Exception as e:
                self.get_logger().error(f"Action '{kind}' error: {e}")

    def _fallback(self):
        self._publish_cognitive("uncertain")
        msg = String()
        msg.data = "Je réfléchis, un instant s'il vous plaît."
        self.pub_say.publish(msg)
        time.sleep(2.0)

    # ──────────────────────────────────────────────────────────────────────────
    # State machine
    # ──────────────────────────────────────────────────────────────────────────

    def _check_state_transition(self):
        hour = datetime.datetime.now().hour

        should_dream = (
            DREAMING_HOUR_START <= hour < DREAMING_HOUR_END
            and self._battery >= DREAMING_BATTERY_THRESHOLD
        )

        with self._lock:
            current = self._state

        if should_dream and current != "DREAMING":
            with self._lock:
                self._state = "DREAMING"
            self._publish_cognitive("dreaming")
            self.get_logger().info("State → DREAMING")

        elif not should_dream and current == "DREAMING":
            with self._lock:
                self._state = "IDLE"
            self._publish_cognitive("idle")
            self.get_logger().info("State → IDLE (dream cycle complete)")

    # ──────────────────────────────────────────────────────────────────────────
    # ChromaDB helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _store_episode(self, user_input: str, parsed: dict):
        if not self._chroma:
            return
        try:
            col = self._chroma.get_collection("episodes_raw")
            doc_id = f"ep_{int(time.time() * 1000)}"
            text = f"User: {user_input}\nThoughts: {parsed.get('thoughts', '')}"
            col.add(
                documents=[text],
                metadatas=[{
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "type": "speech",
                    "emotion_tag": parsed.get("emotion", "neutral"),
                    "importance": "0.5",
                    "reactivation_count": "0",
                    "last_accessed": datetime.datetime.utcnow().isoformat(),
                }],
                ids=[doc_id],
            )
        except Exception as e:
            self.get_logger().warn(f"Episode storage error: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Publisher helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _publish_cognitive(self, state: str):
        msg = String()
        msg.data = state
        self.pub_cognitive.publish(msg)

    def _publish_emotion(self, emotion: str):
        msg = String()
        msg.data = emotion
        self.pub_emotion.publish(msg)


def main():
    rclpy.init()
    node = NAOBrainAgent()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
