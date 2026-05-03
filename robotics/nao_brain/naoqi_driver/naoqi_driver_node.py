#!/usr/bin/env python3
"""
naoqi_driver2 — Nœud ROS2 Humble pour un corps NAO.

Relaie les topics /bodies/{ROBOT_ID}/actuators/* et /naoqi/* vers les topics
/nao/* que nao_ws_bridge.py (embarqué sur NAO) écoute via rosbridge.

NAO initie la connexion WebSocket sortante vers rosbridge (VPS) — aucune
adresse IP ni port du robot n'est requis côté VPS.  La présence de messages
sur /nao/status signale que le robot est connecté.

Flux commandes (cerveau → NAO) :
  /bodies/{id}/actuators/say     → /nao/speech   (std_msgs/String)
  /bodies/{id}/actuators/motion  → /nao/motion   (std_msgs/String JSON)
  /bodies/{id}/actuators/leds    → /nao/leds     (std_msgs/String JSON)
  /bodies/{id}/actuators/posture → /nao/gesture  (std_msgs/String)
  /bodies/{id}/actuators/move    → /nao/cmd_vel  (geometry_msgs/Twist)
  /naoqi/leds                    → /nao/leds     (depuis cognitive_renderer)
  /naoqi/motion                  → /nao/motion   (depuis cognitive_renderer)

Flux capteurs (NAO → cerveau) :
  /nao/status  → /bodies/{id}/sensors/battery  (batterie + heartbeat connexion)
  /nao/audio   → Whisper STT → /bodies/{id}/sensors/speech

Variables d'environnement :
  ROBOT_ID          identifiant de ce corps, défaut "nao_01"
  WHISPER_URL       URL du service Whisper ASR, vide = désactivé
  LISTEN_DURATION   durée d'enregistrement audio en secondes, défaut 4.0
  NAO_STATUS_TIMEOUT secondes sans /nao/status avant de marquer déconnecté, défaut 90
"""

import base64
import json
import os
import threading
import time

import requests
import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from sensor_msgs.msg import Range
from std_msgs.msg import Bool, Float32, String

ROBOT_ID = os.environ.get("ROBOT_ID", "nao_01")
WHISPER_URL = os.environ.get("WHISPER_URL", "")
LISTEN_DURATION = float(os.environ.get("LISTEN_DURATION", "4.0"))
NAO_STATUS_TIMEOUT = float(os.environ.get("NAO_STATUS_TIMEOUT", "90.0"))


class NAOqiDriverNode(Node):

    def __init__(self):
        super().__init__("naoqi_driver2")

        ns = f"/bodies/{ROBOT_ID}"
        self._last_status_time = 0.0
        self._connected = False

        # ── Publishers : capteurs NAO → ROS2 ──────────────────────────────────
        self.pub_speech  = self.create_publisher(String,  f"{ns}/sensors/speech",  10)
        self.pub_battery = self.create_publisher(Float32, f"{ns}/sensors/battery", 10)
        self.pub_bumper  = self.create_publisher(Bool,    f"{ns}/sensors/bumper",  10)
        self.pub_sonar   = self.create_publisher(Range,   f"{ns}/sensors/sonar",   10)
        self.pub_connected = self.create_publisher(Bool,  f"{ns}/connected",       10)

        # ── Publishers : commandes → NAO (via rosbridge → nao_ws_bridge) ──────
        self.pub_nao_speech  = self.create_publisher(String, "/nao/speech",  10)
        self.pub_nao_cmd_vel = self.create_publisher(Twist,  "/nao/cmd_vel", 10)
        self.pub_nao_gesture = self.create_publisher(String, "/nao/gesture", 10)
        self.pub_nao_leds    = self.create_publisher(String, "/nao/leds",    10)
        self.pub_nao_motion  = self.create_publisher(String, "/nao/motion",  10)

        # ── Subscribers : commandes /bodies/*/actuators/* ─────────────────────
        self.create_subscription(String, f"{ns}/actuators/say",     self._on_say,     10)
        self.create_subscription(String, f"{ns}/actuators/motion",  self._on_motion,  10)
        self.create_subscription(String, f"{ns}/actuators/leds",    self._on_leds,    10)
        self.create_subscription(String, f"{ns}/actuators/posture", self._on_posture, 10)
        self.create_subscription(String, f"{ns}/actuators/move",    self._on_move,    10)

        # ── Subscribers : cognitive_renderer → même canaux physiques ──────────
        self.create_subscription(String, "/naoqi/leds",   self._on_naoqi_leds,   10)
        self.create_subscription(String, "/naoqi/motion", self._on_naoqi_motion, 10)

        # ── Subscriber statut NAO : batterie + heartbeat connexion ─────────────
        self.create_subscription(String, "/nao/status", self._on_nao_status, 10)

        # ── Subscriber audio NAO → transcription Whisper ──────────────────────
        self.create_subscription(String, "/nao/audio", self._on_audio, 10)

        # ── Timer heartbeat : détecte déconnexion NAO toutes les 15 s ─────────
        self.create_timer(15.0, self._check_connection)

        self.get_logger().info(
            f"naoqi_driver2 [{ROBOT_ID}] démarré"
            + (f" | Whisper : {WHISPER_URL}" if WHISPER_URL else " | Whisper désactivé")
        )

    # ─────────────────────────────────────────────────────────────────────────
    # NAO → statut batterie + état connexion
    # ─────────────────────────────────────────────────────────────────────────

    def _on_nao_status(self, msg: String):
        self._last_status_time = time.time()
        try:
            data = json.loads(msg.data)
            level = float(data.get("battery", -1))
            bat = Float32()
            bat.data = level
            self.pub_battery.publish(bat)
        except (json.JSONDecodeError, ValueError):
            pass
        if not self._connected:
            self._connected = True
            self._publish_connected(True)
            self.get_logger().info(f"[{ROBOT_ID}] NAO connecté via rosbridge")

    def _check_connection(self):
        if self._last_status_time == 0.0:
            return
        elapsed = time.time() - self._last_status_time
        if elapsed > NAO_STATUS_TIMEOUT:
            if self._connected:
                self._connected = False
                self._publish_connected(False)
                self.get_logger().warn(
                    f"[{ROBOT_ID}] Pas de /nao/status depuis {elapsed:.0f}s — déconnecté"
                )
        elif self._connected:
            # Republication périodique : body_gateway reçoit l'état dans les 15 s
            # après son (re)démarrage, même si naoqi_driver n'a pas changé d'état.
            self._publish_connected(True)

    def _publish_connected(self, state: bool):
        msg = Bool()
        msg.data = state
        self.pub_connected.publish(msg)

    # ─────────────────────────────────────────────────────────────────────────
    # Audio NAO → Whisper STT → topic speech
    # ─────────────────────────────────────────────────────────────────────────

    def _on_audio(self, msg: String):
        if not WHISPER_URL:
            return
        threading.Thread(
            target=self._transcribe_and_publish,
            args=(msg.data,),
            daemon=True,
        ).start()

    def _transcribe_and_publish(self, b64_audio: str):
        try:
            audio_bytes = base64.b64decode(b64_audio)
            resp = requests.post(
                f"{WHISPER_URL}/asr?task=transcribe&language=fr&output=json",
                files={"audio_file": ("audio.wav", audio_bytes, "audio/wav")},
                timeout=30,
            )
            if not resp.ok:
                self.get_logger().warn(f"Whisper erreur {resp.status_code}")
                return
            text = resp.json().get("text", "").strip()
            if text and len(text) > 3:
                self.get_logger().info(f"STT [{ROBOT_ID}]: {text}")
                out = String()
                out.data = text
                self.pub_speech.publish(out)
            else:
                self.get_logger().info("Transcription vide — rien à envoyer")
        except Exception as e:
            self.get_logger().warn(f"_transcribe_and_publish error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # Actuateurs /bodies/*/actuators/* → /nao/* (rosbridge → nao_ws_bridge)
    # ─────────────────────────────────────────────────────────────────────────

    def _on_say(self, msg: String):
        self.get_logger().info(f"[{ROBOT_ID}] → /nao/speech : {msg.data[:80]!r}")
        self.pub_nao_speech.publish(msg)

    def _on_motion(self, msg: String):
        self.get_logger().info(f"[{ROBOT_ID}] → /nao/motion : {msg.data[:80]!r}")
        self.pub_nao_motion.publish(msg)

    def _on_leds(self, msg: String):
        self.get_logger().info(f"[{ROBOT_ID}] → /nao/leds")
        self.pub_nao_leds.publish(self._normalize_leds(msg.data))

    def _on_posture(self, msg: String):
        self.get_logger().info(f"[{ROBOT_ID}] → /nao/gesture : {msg.data!r}")
        self.pub_nao_gesture.publish(msg)

    def _on_move(self, msg: String):
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().error(f"move JSON invalide: {e}")
            return
        twist = Twist()
        twist.linear.x  = float(data.get("x",     0.0))
        twist.linear.y  = float(data.get("y",     0.0))
        twist.angular.z = float(data.get("theta", 0.0))
        self.pub_nao_cmd_vel.publish(twist)

    # ─────────────────────────────────────────────────────────────────────────
    # cognitive_renderer /naoqi/* → mêmes canaux physiques /nao/*
    # ─────────────────────────────────────────────────────────────────────────

    def _on_naoqi_leds(self, msg: String):
        self.pub_nao_leds.publish(self._normalize_leds(msg.data))

    def _on_naoqi_motion(self, msg: String):
        self.pub_nao_motion.publish(msg)

    # ─────────────────────────────────────────────────────────────────────────
    # Helper
    # ─────────────────────────────────────────────────────────────────────────

    def _normalize_leds(self, raw: str) -> String:
        """Renomme 'group' → 'name' dans le JSON LEDs pour nao_ws_bridge."""
        try:
            data = json.loads(raw)
            if "group" in data and "name" not in data:
                data["name"] = data.pop("group")
            raw = json.dumps(data)
        except (json.JSONDecodeError, AttributeError):
            pass
        out = String()
        out.data = raw
        return out

    def destroy_node(self):
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
