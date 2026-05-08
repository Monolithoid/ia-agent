#!/usr/bin/env python3
"""
naoqi_driver2 — ROS 2 node for a single NAO body.

Bridges /bodies/{ROBOT_ID}/actuators/* and /naoqi/* to the /nao/* topics
that nao_ws_bridge.py (embedded on the robot) subscribes to via rosbridge.

The NAO initiates an outbound WebSocket connection to rosbridge (VPS) — no
robot IP or open port is required on the VPS side.  Presence of /nao/status
messages indicates the robot is connected.

Command flow (brain → NAO):
  /bodies/{id}/actuators/say     → /nao/speech   (std_msgs/String)
  /bodies/{id}/actuators/motion  → /nao/motion   (std_msgs/String JSON)
  /bodies/{id}/actuators/leds    → /nao/leds     (std_msgs/String JSON)
  /bodies/{id}/actuators/posture → /nao/gesture  (std_msgs/String)
  /bodies/{id}/actuators/move    → /nao/cmd_vel  (geometry_msgs/Twist)
  /naoqi/leds                    → /nao/leds      (from cognitive_renderer)
  /naoqi/motion                  → /nao/motion    (from cognitive_renderer)
  /naoqi/stiffness               → /nao/stiffness (from cognitive_renderer via energy_state)

Sensor flow (NAO → brain):
  /nao/status  → /bodies/{id}/sensors/battery  (battery level + connection heartbeat)
  /nao/audio   → Whisper STT → /bodies/{id}/sensors/speech

Environment variables:
  ROBOT_ID           body identifier, default "nao_01"
  WHISPER_URL        Whisper ASR service URL, empty = disabled
  LISTEN_DURATION    audio recording duration in seconds, default 4.0
  NAO_STATUS_TIMEOUT seconds without /nao/status before marking disconnected, default 90
  TTS_VOICE          edge-tts voice name, default "fr-FR-DeniseNeural"
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
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import Range
from std_msgs.msg import Bool, Float32, String

ROBOT_ID = os.environ.get("ROBOT_ID", "nao_01")
WHISPER_URL = os.environ.get("WHISPER_URL", "")
LISTEN_DURATION = float(os.environ.get("LISTEN_DURATION", "4.0"))
NAO_STATUS_TIMEOUT = float(os.environ.get("NAO_STATUS_TIMEOUT", "90.0"))
TTS_VOICE = os.environ.get("TTS_VOICE", "fr-FR-DeniseNeural")


class NAOqiDriverNode(Node):

    def __init__(self):
        super().__init__("naoqi_driver2")

        ns = f"/bodies/{ROBOT_ID}"
        self._last_status_time = 0.0
        self._connected = False

        # ── Publishers: NAO sensors → ROS 2 ──────────────────────────────────
        self.pub_speech  = self.create_publisher(String,  f"{ns}/sensors/speech",  10)
        self.pub_battery = self.create_publisher(Float32, f"{ns}/sensors/battery", 10)
        self.pub_bumper  = self.create_publisher(Bool,    f"{ns}/sensors/bumper",  10)
        self.pub_sonar   = self.create_publisher(Range,   f"{ns}/sensors/sonar",   10)
        self.pub_connected = self.create_publisher(Bool,  f"{ns}/connected",       10)

        # ── Publishers: commands → NAO (via rosbridge → nao_ws_bridge) ───────
        self.pub_nao_speech     = self.create_publisher(String, "/nao/speech",     10)
        self.pub_nao_audio_out  = self.create_publisher(String, "/nao/audio_out",  10)
        self.pub_nao_cmd_vel    = self.create_publisher(Twist,  "/nao/cmd_vel",    10)
        self.pub_nao_gesture    = self.create_publisher(String, "/nao/gesture",    10)
        self.pub_nao_leds       = self.create_publisher(String, "/nao/leds",       10)
        self.pub_nao_motion     = self.create_publisher(String, "/nao/motion",     10)
        self.pub_nao_stiffness  = self.create_publisher(String, "/nao/stiffness",  10)

        # ── Subscribers: actuator commands from body_gateway ─────────────────
        self.create_subscription(String, f"{ns}/actuators/say",     self._on_say,     10)
        self.create_subscription(String, f"{ns}/actuators/motion",  self._on_motion,  10)
        self.create_subscription(String, f"{ns}/actuators/leds",    self._on_leds,    10)
        self.create_subscription(String, f"{ns}/actuators/posture", self._on_posture, 10)
        self.create_subscription(String, f"{ns}/actuators/move",    self._on_move,    10)

        # ── Subscribers: cognitive_renderer → same physical channels ─────────
        self.create_subscription(String, "/naoqi/leds",      self._on_naoqi_leds,      10)
        self.create_subscription(String, "/naoqi/motion",    self._on_naoqi_motion,    10)
        self.create_subscription(String, "/naoqi/stiffness", self._on_naoqi_stiffness, 10)

        # ── Subscriber: TTS capability negotiation ────────────────────────────
        self.create_subscription(String, "/nao/capabilities", self._on_capabilities, 10)
        # Default TTS method before the robot announces its capabilities
        self._tts_method = "mp3" if TTS_VOICE else "text"

        # ── Subscriber: NAO status heartbeat (battery + connection state) ─────
        # rosbridge forwards /nao/status as TRANSIENT_LOCAL — the subscriber
        # must use matching durability to receive messages after reconnect.
        _transient_qos = QoSProfile(
            depth=10,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(String, "/nao/status", self._on_nao_status, _transient_qos)

        # ── Subscriber: NAO audio → Whisper STT ──────────────────────────────
        self.create_subscription(String, "/nao/audio", self._on_audio, 10)

        # ── Timer: detect NAO disconnection every 15 s ────────────────────────
        self.create_timer(15.0, self._check_connection)

        self.get_logger().info(
            f"naoqi_driver2 [{ROBOT_ID}] démarré"
            + (f" | Whisper : {WHISPER_URL}" if WHISPER_URL else " | Whisper désactivé")
        )

    # ─────────────────────────────────────────────────────────────────────────
    # NAO → battery status + connection state
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
            # Periodic re-publish so body_gateway receives the state within 15 s
            # after its own (re)start even when naoqi_driver has not changed state.
            self._publish_connected(True)

    def _publish_connected(self, state: bool):
        msg = Bool()
        msg.data = state
        self.pub_connected.publish(msg)

    # ─────────────────────────────────────────────────────────────────────────
    # NAO audio → Whisper STT → /bodies/{id}/sensors/speech
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
    # Actuators /bodies/*/actuators/* → /nao/* (rosbridge → nao_ws_bridge)
    # ─────────────────────────────────────────────────────────────────────────

    def _on_capabilities(self, msg: String):
        try:
            cap = json.loads(msg.data)
            method = cap.get("tts_method")
            if method in ("mp3", "text"):
                self._tts_method = method
                self.get_logger().info(f"[{ROBOT_ID}] TTS method négocié : {method}")
        except json.JSONDecodeError:
            pass

    def _on_say(self, msg: String):
        text = msg.data
        self.get_logger().info(
            f"[{ROBOT_ID}] → TTS ({self._tts_method}) : {text[:80]!r}"
        )
        if self._tts_method == "mp3" and TTS_VOICE:
            threading.Thread(target=self._tts_and_publish, args=(text,), daemon=True).start()
        else:
            self.pub_nao_speech.publish(msg)

    def _tts_and_publish(self, text: str):
        import asyncio
        import io
        try:
            import edge_tts
        except ImportError:
            self.get_logger().warn("edge-tts non installé — fallback texte NAOqi")
            out = String()
            out.data = text
            self.pub_nao_speech.publish(out)
            return
        try:
            async def _gen():
                communicate = edge_tts.Communicate(text, TTS_VOICE)
                buf = io.BytesIO()
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        buf.write(chunk["data"])
                return buf.getvalue()

            loop = asyncio.new_event_loop()
            audio_mp3 = loop.run_until_complete(_gen())
            loop.close()
            b64 = base64.b64encode(audio_mp3).decode()
            out = String()
            out.data = b64
            self.pub_nao_audio_out.publish(out)
            self.get_logger().info(f"TTS audio envoyé ({len(audio_mp3)} octets MP3)")
        except Exception as e:
            self.get_logger().warn(f"edge-tts erreur: {e} — fallback texte NAOqi")
            out = String()
            out.data = text
            self.pub_nao_speech.publish(out)

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
    # cognitive_renderer /naoqi/* → same physical /nao/* channels
    # ─────────────────────────────────────────────────────────────────────────

    def _on_naoqi_leds(self, msg: String):
        self.pub_nao_leds.publish(self._normalize_leds(msg.data))

    def _on_naoqi_motion(self, msg: String):
        self.pub_nao_motion.publish(msg)

    def _on_naoqi_stiffness(self, msg: String):
        """Forward stiffness commands from cognitive_renderer to the bridge.

        Input format (cognitive_renderer):
          {"groups": [{"name": "head", "value": 1.0}, ...]}
        Output format (nao_ws_bridge /nao/stiffness):
          published one message per group: {"groups": ["head"], "value": 1.0}
        """
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        for entry in data.get("groups", []):
            name  = entry.get("name")
            value = entry.get("value")
            if name is None or value is None:
                continue
            out = String()
            out.data = json.dumps({"groups": [name], "value": float(value)})
            self.pub_nao_stiffness.publish(out)

    # ─────────────────────────────────────────────────────────────────────────
    # Helper
    # ─────────────────────────────────────────────────────────────────────────

    def _normalize_leds(self, raw: str) -> String:
        """Rename 'group' → 'name' in the LED JSON payload expected by nao_ws_bridge."""
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
