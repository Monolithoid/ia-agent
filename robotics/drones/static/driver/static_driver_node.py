#!/usr/bin/env python3
"""
static_driver — ROS 2 node for a single static drone (ADR 004).

Bridges the /drones/{DRONE_ID}/* topics that static_drone.py (embedded on the
Raspberry Pi or Ubuntu PC) publishes via rosbridge to the generic
/bodies/{DRONE_ID}/* topics that body_gateway understands.

The drone initiates an outbound WebSocket connection to rosbridge (VPS), like
the NAO.  Presence of /drones/{id}/status messages indicates it is connected.

Sensor flow (drone → brain):
  /drones/{id}/audio    (String JSON {wav, azimuth_deg, direction_source})
                        → /bodies/{id}/sensors/sound_direction (String JSON {azimuth_deg, source})
                        → Whisper STT → /bodies/{id}/sensors/speech (String)
  /drones/{id}/image    (String JSON {jpeg, width, height, reason})
                        → /bodies/{id}/sensors/image (same JSON, relayed as-is)
  /drones/{id}/speaking (Bool) → /bodies/{id}/sensors/speaking (Bool)
  /drones/{id}/status   (String JSON, every 30 s) → /bodies/{id}/connected (Bool)

Command flow (brain → drone):
  /bodies/{id}/actuators/say → edge-tts → /drones/{id}/audio_out (String, MP3 base64)

Also started for obs_* bodies (ADR 005): the OBS stream voice uses the same
say → edge-tts → audio_out path and the same status / speaking topics.

Environment variables:
  DRONE_ID              body identifier, default "static_01"
  WHISPER_URL           Whisper ASR service URL, empty = disabled
  TTS_VOICE             edge-tts voice name, default "fr-FR-DeniseNeural"
  DRONE_STATUS_TIMEOUT  seconds without message before marking disconnected, default 90
"""

import base64
import json
import os
import sys
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import Bool, String

try:
    import speech_services
except ImportError:
    # Lancé depuis le dépôt : le module partagé est dans robotics/shared/.
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "..", "shared"))
    import speech_services

DRONE_ID = os.environ.get("DRONE_ID", "static_01")
WHISPER_URL = os.environ.get("WHISPER_URL", "")
TTS_VOICE = os.environ.get("TTS_VOICE", "fr-FR-DeniseNeural")
DRONE_STATUS_TIMEOUT = float(os.environ.get("DRONE_STATUS_TIMEOUT", "90.0"))


class StaticDriverNode(Node):

    def __init__(self):
        super().__init__(f"static_driver_{DRONE_ID}")

        ns = f"/bodies/{DRONE_ID}"
        drone = f"/drones/{DRONE_ID}"
        self._last_seen = 0.0
        self._connected = False

        # ── Publishers: drone sensors → ROS 2 ────────────────────────────────
        self.pub_speech    = self.create_publisher(String, f"{ns}/sensors/speech",          10)
        self.pub_direction = self.create_publisher(String, f"{ns}/sensors/sound_direction", 10)
        self.pub_image     = self.create_publisher(String, f"{ns}/sensors/image",           10)
        self.pub_speaking  = self.create_publisher(Bool,   f"{ns}/sensors/speaking",        10)
        self.pub_connected = self.create_publisher(Bool,   f"{ns}/connected",               10)

        # ── Publisher: commands → drone (via rosbridge → static_drone.py) ────
        self.pub_audio_out = self.create_publisher(String, f"{drone}/audio_out", 10)

        # ── Subscriber: actuator commands from body_gateway ──────────────────
        self.create_subscription(String, f"{ns}/actuators/say", self._on_say, 10)

        # ── Subscribers: drone topics (rosbridge) ────────────────────────────
        # Same QoS as /nao/status in naoqi_driver: rosbridge forwards the
        # heartbeat as TRANSIENT_LOCAL.
        _transient_qos = QoSProfile(
            depth=10,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(String, f"{drone}/status",   self._on_status,   _transient_qos)
        self.create_subscription(String, f"{drone}/audio",    self._on_audio,    10)
        self.create_subscription(String, f"{drone}/image",    self._on_image,    10)
        self.create_subscription(Bool,   f"{drone}/speaking", self._on_speaking, 10)

        # ── Timer: detect drone disconnection every 15 s ──────────────────────
        self.create_timer(15.0, self._check_connection)

        self.get_logger().info(
            f"static_driver [{DRONE_ID}] démarré"
            + (f" | Whisper : {WHISPER_URL}" if WHISPER_URL else " | Whisper désactivé")
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Connection state
    # ─────────────────────────────────────────────────────────────────────────

    def _seen(self):
        """Any message from the drone proves it is connected."""
        self._last_seen = time.time()
        if not self._connected:
            self._connected = True
            self._publish_connected(True)
            self.get_logger().info(f"[{DRONE_ID}] Drone statique connecté via rosbridge")

    def _on_status(self, msg: String):
        self._seen()

    def _check_connection(self):
        """Same logic as naoqi_driver: mark disconnected after DRONE_STATUS_TIMEOUT
        seconds of silence, otherwise re-publish the connected state so a
        restarted body_gateway learns it within 15 s."""
        # Nothing received yet — drone has not connected, nothing to check.
        if self._last_seen == 0.0:
            return
        elapsed = time.time() - self._last_seen
        if elapsed > DRONE_STATUS_TIMEOUT:
            if self._connected:
                self._connected = False
                self._publish_connected(False)
                self.get_logger().warn(
                    f"[{DRONE_ID}] Rien reçu du drone depuis {elapsed:.0f}s — déconnecté"
                )
        elif self._connected:
            self._publish_connected(True)

    def _publish_connected(self, state: bool):
        msg = Bool()
        msg.data = state
        self.pub_connected.publish(msg)

    # ─────────────────────────────────────────────────────────────────────────
    # Drone audio → sound direction + Whisper STT
    # ─────────────────────────────────────────────────────────────────────────

    def _on_audio(self, msg: String):
        """Receive one speech segment (WAV base64 + angle) detected by the drone.

        The direction is published at once, before the transcription, so
        body_gateway already knows it when the text arrives.  Transcription
        runs in a daemon thread to keep the ROS spin loop free.
        """
        self._seen()
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f"audio JSON invalide: {e}")
            return

        direction = String()
        direction.data = json.dumps({
            "azimuth_deg": data.get("azimuth_deg"),
            "source": data.get("direction_source", "none"),
        })
        self.pub_direction.publish(direction)

        # Whisper disabled by config — audio received but not transcribed.
        if not WHISPER_URL or not data.get("wav"):
            return
        threading.Thread(
            target=self._transcribe_and_publish,
            args=(data["wav"],),
            daemon=True,
        ).start()

    def _transcribe_and_publish(self, b64_audio: str):
        try:
            text = speech_services.transcribe(WHISPER_URL, base64.b64decode(b64_audio))
        except speech_services.WhisperError as e:
            self.get_logger().warn(str(e))
            return
        except Exception as e:
            self.get_logger().warn(f"_transcribe_and_publish error: {e}")
            return
        if speech_services.is_meaningful(text):
            self.get_logger().info(f"STT [{DRONE_ID}]: {text}")
            out = String()
            out.data = text
            self.pub_speech.publish(out)
        else:
            self.get_logger().info("Transcription vide — rien à envoyer")

    # ─────────────────────────────────────────────────────────────────────────
    # Image and speaking state — relayed as-is
    # ─────────────────────────────────────────────────────────────────────────

    def _on_image(self, msg: String):
        self._seen()
        self.pub_image.publish(msg)

    def _on_speaking(self, msg: Bool):
        self._seen()
        self.pub_speaking.publish(msg)

    # ─────────────────────────────────────────────────────────────────────────
    # /say → edge-tts → /drones/{id}/audio_out
    # ─────────────────────────────────────────────────────────────────────────

    def _on_say(self, msg: String):
        text = msg.data.strip()
        if not text:
            return
        self.get_logger().info(f"[{DRONE_ID}] → TTS : {text[:80]!r}")
        threading.Thread(target=self._tts_and_publish, args=(text,), daemon=True).start()

    def _tts_and_publish(self, text: str):
        # The drone has no local speech synthesis: without edge-tts nothing is said.
        try:
            audio_mp3 = speech_services.synthesize_mp3(text, TTS_VOICE)
        except ImportError:
            self.get_logger().error("edge-tts non installé — phrase perdue")
            return
        except Exception as e:
            self.get_logger().warn(f"edge-tts erreur: {e} — phrase perdue")
            return
        out = String()
        out.data = base64.b64encode(audio_mp3).decode()
        self.pub_audio_out.publish(out)
        self.get_logger().info(f"TTS audio envoyé ({len(audio_mp3)} octets MP3)")


def main():
    rclpy.init()
    node = StaticDriverNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
