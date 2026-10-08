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
  /nao/status  → /bodies/{id}/sensors/battery     (Float32 — for logic: dreaming threshold)
               → /bodies/{id}/sensors/state      (JSON — all other sensor readings for LLM)
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
        self.pub_speech       = self.create_publisher(String,  f"{ns}/sensors/speech",  10)
        self.pub_battery      = self.create_publisher(Float32, f"{ns}/sensors/battery", 10)
        self.pub_sensor_state = self.create_publisher(String,  f"{ns}/sensors/state",   10)
        self.pub_bumper       = self.create_publisher(Bool,    f"{ns}/sensors/bumper",  10)
        self.pub_sonar        = self.create_publisher(Range,   f"{ns}/sensors/sonar",   10)
        self.pub_connected    = self.create_publisher(Bool,    f"{ns}/connected",       10)

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
        """Process the 30 s heartbeat published by the robot.

        Updates the last-seen timestamp (used by _check_connection to detect
        disconnection) and publishes battery + sensor state topics for the LLM.
        Also transitions to connected state on the first heartbeat received.
        """
        self._last_status_time = time.time()
        try:
            data = json.loads(msg.data)

            # Battery as Float32 — kept separate for logic (dreaming threshold etc.)
            level = float(data.get("battery", -1))
            bat = Float32()
            bat.data = level
            self.pub_battery.publish(bat)

            # All other sensor readings consolidated into one JSON topic for the LLM
            state: dict = {}
            # Motor temperatures are present only when the NAO firmware reports them.
            if data.get("motor_temp"):
                state["motor_temp"] = data["motor_temp"]
            # Environment block may contain ambient_light, sound_level, etc.
            if data.get("environment"):
                state.update(data["environment"])
            # Only publish if there is at least one sensor value to report.
            if state:
                out = String()
                out.data = json.dumps(state)
                self.pub_sensor_state.publish(out)

        except (json.JSONDecodeError, ValueError):
            pass
        # First heartbeat after (re)start transitions the driver to connected state.
        if not self._connected:
            self._connected = True
            self._publish_connected(True)
            self.get_logger().info(f"[{ROBOT_ID}] NAO connecté via rosbridge")

    def _check_connection(self):
        """Detect robot disconnection from the heartbeat silence.

        Called every 15 s by the ROS 2 timer.  If no /nao/status has arrived
        within NAO_STATUS_TIMEOUT seconds, the robot is considered offline.
        Also re-publishes the connected state periodically so body_gateway
        learns the state within 15 s of its own restart.
        """
        # No heartbeat ever received — robot has not connected yet, nothing to check.
        if self._last_status_time == 0.0:
            return
        elapsed = time.time() - self._last_status_time
        if elapsed > NAO_STATUS_TIMEOUT:
            # Heartbeat silent for too long — mark as disconnected and alert.
            if self._connected:
                self._connected = False
                self._publish_connected(False)
                self.get_logger().warn(
                    f"[{ROBOT_ID}] Pas de /nao/status depuis {elapsed:.0f}s — déconnecté"
                )
        elif self._connected:
            # Robot is connected and heartbeat is current — re-publish so any
            # newly started subscriber (e.g. body_gateway) gets the current state.
            self._publish_connected(True)

    def _publish_connected(self, state: bool):
        msg = Bool()
        msg.data = state
        self.pub_connected.publish(msg)

    # ─────────────────────────────────────────────────────────────────────────
    # NAO audio → Whisper STT → /bodies/{id}/sensors/speech
    # ─────────────────────────────────────────────────────────────────────────

    def _on_audio(self, msg: String):
        """Receive base64-encoded WAV audio from the robot and transcribe it.

        Audio arrives after a wake-word detection on the NAO side.
        Transcription is offloaded to a daemon thread so the ROS spin
        loop is not blocked during the Whisper HTTP call.
        """
        # Whisper disabled by config — audio received but not transcribed.
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
            # Whisper returned an HTTP error — log and abort without crashing.
            if not resp.ok:
                self.get_logger().warn(f"Whisper erreur {resp.status_code}")
                return
            text = resp.json().get("text", "").strip()
            # Accept transcription only if it contains meaningful content
            # (> 3 chars filters out noise artefacts like "." or "ok").
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
        """Receive the robot's TTS capability announcement on connect.

        The robot publishes its preferred TTS method once after establishing
        the rosbridge connection.  We update the local method so subsequent
        /say commands use the right audio path.
        """
        try:
            cap = json.loads(msg.data)
            method = cap.get("tts_method")
            # Only accept known methods — guard against malformed capability messages.
            if method in ("mp3", "text"):
                self._tts_method = method
                self.get_logger().info(f"[{ROBOT_ID}] TTS method négocié : {method}")
        except json.JSONDecodeError:
            pass

    def _on_say(self, msg: String):
        """Route a speech text to the appropriate TTS path.

        mp3 path: generate audio via edge-tts on the VPS, send base64 MP3
                  on /nao/audio_out → NAOqi ALAudioPlayer (better voice quality).
        text path: send raw text on /nao/speech → NAOqi ALTextToSpeech (fallback).
        """
        text = msg.data
        self.get_logger().info(
            f"[{ROBOT_ID}] → TTS ({self._tts_method}) : {text[:80]!r}"
        )
        # Use edge-tts audio generation when both the mp3 method is selected
        # AND a voice name is configured in TTS_VOICE.
        if self._tts_method == "mp3" and TTS_VOICE:
            threading.Thread(target=self._tts_and_publish, args=(text,), daemon=True).start()
        else:
            # Fallback: send raw text for native NAOqi TTS synthesis on the robot.
            self.pub_nao_speech.publish(msg)

    def _tts_and_publish(self, text: str):
        import asyncio
        import io
        try:
            import edge_tts
        except ImportError:
            # edge-tts not available in this environment — fall back to native TTS.
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
                    # Only audio chunks contain binary data — skip metadata events.
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
            # Skip malformed entries that are missing either the group name or value.
            if name is None or value is None:
                continue
            out = String()
            out.data = json.dumps({"groups": [name], "value": float(value)})
            self.pub_nao_stiffness.publish(out)

    # ─────────────────────────────────────────────────────────────────────────
    # Helper
    # ─────────────────────────────────────────────────────────────────────────

    def _normalize_leds(self, raw: str) -> String:
        """Rename 'group' → 'name' in the LED JSON payload expected by nao_ws_bridge.

        body_gateway publishes LED commands with key 'group' (OpenAPI convention).
        nao_ws_bridge expects 'name' (NAOqi ALLeds.fadeRGB convention).
        This normalisation is done here so both body_gateway and cognitive_renderer
        can use their own field names without knowing about the bridge's format.
        """
        try:
            data = json.loads(raw)
            # Only rename if 'group' is present and 'name' is not already set,
            # to avoid overwriting an already-correct payload.
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
