#!/usr/bin/env python3
"""
naoqi_driver2 — Nœud ROS2 Humble pour un corps NAO.

Traduit les topics /bodies/{ROBOT_ID}/* en appels HTTP vers naoqi_bridge,
et publie les événements NAO (parole, sonar, batterie, bumper) vers le même
namespace — consommés par body_gateway.

Chaque instance de ce nœud gère UN corps physique identifié par ROBOT_ID.
Pour plusieurs NAO, lancer plusieurs instances avec des ROBOT_ID distincts
(et des NAOQI_BRIDGE_URL distincts si plusieurs bridges).

Topics publiés (NAO → body_gateway) :
  /bodies/{ROBOT_ID}/sensors/speech    std_msgs/String
  /bodies/{ROBOT_ID}/sensors/sonar     sensor_msgs/Range
  /bodies/{ROBOT_ID}/sensors/battery   std_msgs/Float32
  /bodies/{ROBOT_ID}/sensors/bumper    std_msgs/Bool

Topics souscrits (body_gateway → NAO) :
  /bodies/{ROBOT_ID}/actuators/say     std_msgs/String
  /bodies/{ROBOT_ID}/actuators/motion  std_msgs/String  (JSON)
  /bodies/{ROBOT_ID}/actuators/leds    std_msgs/String  (JSON)
  /bodies/{ROBOT_ID}/actuators/posture std_msgs/String
  /bodies/{ROBOT_ID}/actuators/move    std_msgs/String  (JSON)

Variables d'environnement :
  ROBOT_ID          identifiant de ce corps, défaut "nao_01"
  NAOQI_BRIDGE_URL  URL du bridge Python 2.7, défaut http://localhost:8090
"""

import json
import os
import threading
import time

import requests
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Range
from std_msgs.msg import Bool, Float32, String

ROBOT_ID = os.environ.get("ROBOT_ID", "nao_01")
BRIDGE_URL = os.environ.get("NAOQI_BRIDGE_URL", "http://localhost:8090")
WHISPER_URL = os.environ.get("WHISPER_URL", "")
LISTEN_DURATION = float(os.environ.get("LISTEN_DURATION", "4.0"))
SSE_RECONNECT_DELAY = 5


class NAOqiDriverNode(Node):

    def __init__(self):
        super().__init__("naoqi_driver2")

        ns = f"/bodies/{ROBOT_ID}"

        # ── Publishers : capteurs NAO → ROS2 ──────────────────────────────────
        self.pub_speech  = self.create_publisher(String,  f"{ns}/sensors/speech",  10)
        self.pub_battery = self.create_publisher(Float32, f"{ns}/sensors/battery", 10)
        self.pub_bumper  = self.create_publisher(Bool,    f"{ns}/sensors/bumper",  10)
        self.pub_sonar   = self.create_publisher(Range,   f"{ns}/sensors/sonar",   10)

        # ── Subscribers : commandes ROS2 → NAO ───────────────────────────────
        self.create_subscription(String, f"{ns}/actuators/say",     self._on_say,     10)
        self.create_subscription(String, f"{ns}/actuators/motion",  self._on_motion,  10)
        self.create_subscription(String, f"{ns}/actuators/leds",    self._on_leds,    10)
        self.create_subscription(String, f"{ns}/actuators/posture", self._on_posture, 10)
        self.create_subscription(String, f"{ns}/actuators/move",    self._on_move,    10)

        # ── Stream SSE depuis naoqi_bridge ────────────────────────────────────
        self._sse_running = True
        self._tts_cooldown_until = 0.0
        threading.Thread(target=self._sse_loop, daemon=True).start()

        if WHISPER_URL:
            self.get_logger().info(
                f"naoqi_driver2 [{ROBOT_ID}] Wake word actif → enregistrement via Whisper : {WHISPER_URL}"
            )

        self.get_logger().info(
            f"naoqi_driver2 [{ROBOT_ID}] démarré — bridge : {BRIDGE_URL}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # SSE : événements NAO → topics ROS2
    # ─────────────────────────────────────────────────────────────────────────

    def _sse_loop(self):
        while self._sse_running:
            try:
                with requests.get(f"{BRIDGE_URL}/events", stream=True, timeout=None) as resp:
                    resp.raise_for_status()
                    self.get_logger().info("SSE connecté")
                    for raw_line in resp.iter_lines():
                        if not self._sse_running:
                            break
                        if raw_line and raw_line.startswith(b"data:"):
                            payload = raw_line[5:].strip()
                            if payload:
                                self._dispatch_event(json.loads(payload))
            except Exception as e:
                if self._sse_running:
                    self.get_logger().warn(
                        f"SSE déconnecté : {e} — reconnexion dans {SSE_RECONNECT_DELAY}s"
                    )
                    time.sleep(SSE_RECONNECT_DELAY)

    def _dispatch_event(self, event: dict):
        topic = event.get("topic", "")

        if topic == "wake_word_detected":
            if not WHISPER_URL:
                return
            cooldown = self._tts_cooldown_until - time.time()
            if cooldown > 0:
                self.get_logger().info(
                    f"Wake word ignoré — TTS en cours ({cooldown:.1f}s restantes)"
                )
                return
            threading.Thread(target=self._on_wake_word, daemon=True).start()

        elif topic == "speech_recognized":
            msg = String()
            msg.data = event.get("data", "")
            self.pub_speech.publish(msg)

        elif topic == "battery":
            msg = Float32()
            msg.data = float(event.get("data", 0))
            self.pub_battery.publish(msg)

        elif topic == "bumper":
            msg = Bool()
            msg.data = bool(event.get("data", False))
            self.pub_bumper.publish(msg)

        elif topic == "sonar":
            msg = Range()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.header.frame_id = f"sonar_{event.get('sensor', 'front')}"
            msg.radiation_type = Range.ULTRASOUND
            msg.field_of_view = 0.523
            msg.min_range = 0.20
            msg.max_range = 2.55
            msg.range = float(event.get("data", 2.55))
            self.pub_sonar.publish(msg)

    # ─────────────────────────────────────────────────────────────────────────
    # Commandes ROS2 → naoqi_bridge HTTP
    # ─────────────────────────────────────────────────────────────────────────

    def _post(self, endpoint: str, payload: dict):
        try:
            resp = requests.post(f"{BRIDGE_URL}{endpoint}", json=payload, timeout=10)
            if not resp.ok:
                self.get_logger().error(
                    f"Bridge {endpoint} error {resp.status_code}: {resp.text}"
                )
        except requests.RequestException as e:
            self.get_logger().error(f"Bridge {endpoint} request failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Wake word → STT Whisper → speech topic
    # ─────────────────────────────────────────────────────────────────────────

    def _on_wake_word(self):
        """Déclenché après détection de 'ok nao' : enregistre l'audio, transcrit via
        Whisper et publie le texte sur le topic speech pour body_gateway."""
        self.get_logger().info(f"[{ROBOT_ID}] Wake word détecté — enregistrement {LISTEN_DURATION}s …")
        try:
            resp = requests.post(
                f"{BRIDGE_URL}/listen",
                json={"duration": LISTEN_DURATION},
                timeout=LISTEN_DURATION + 15,
            )
            if not resp.ok or not resp.content:
                self.get_logger().warn("Enregistrement audio vide ou erreur bridge")
                return

            whisper_resp = requests.post(
                f"{WHISPER_URL}/asr?task=transcribe&language=fr&output=json",
                files={"audio_file": ("audio.wav", resp.content, "audio/wav")},
                timeout=30,
            )
            if not whisper_resp.ok:
                self.get_logger().warn(f"Whisper erreur {whisper_resp.status_code}")
                return

            text = whisper_resp.json().get("text", "").strip()
            if text and len(text) > 3:
                self.get_logger().info(f"STT [{ROBOT_ID}]: {text}")
                msg = String()
                msg.data = text
                self.pub_speech.publish(msg)
            else:
                self.get_logger().info("Transcription vide — rien à envoyer")

        except Exception as e:
            self.get_logger().warn(f"_on_wake_word error: {e}")

    def _on_say(self, msg: String):
        # Bloque l'écoute pendant la durée estimée du TTS + marge
        word_count = len(msg.data.split())
        estimated_tts = max(3.0, word_count * 0.35) + 1.5
        self._tts_cooldown_until = time.time() + estimated_tts
        self._post("/say", {"text": msg.data})

    def _on_motion(self, msg: String):
        try:
            self._post("/motion", json.loads(msg.data))
        except json.JSONDecodeError as e:
            self.get_logger().error(f"motion JSON invalide: {e}")

    def _on_leds(self, msg: String):
        try:
            self._post("/leds", json.loads(msg.data))
        except json.JSONDecodeError as e:
            self.get_logger().error(f"leds JSON invalide: {e}")

    def _on_posture(self, msg: String):
        self._post("/posture", {"posture": msg.data})

    def _on_move(self, msg: String):
        try:
            self._post("/move", json.loads(msg.data))
        except json.JSONDecodeError as e:
            self.get_logger().error(f"move JSON invalide: {e}")

    def destroy_node(self):
        self._sse_running = False
        super().destroy_node()


def main():
    rclpy.init()
    node = NAOqiDriverNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
