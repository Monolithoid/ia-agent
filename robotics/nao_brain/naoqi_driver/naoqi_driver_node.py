#!/usr/bin/env python3
"""
naoqi_driver2 — ROS2 Humble node bridging /naoqi/* topics ↔ naoqi_bridge HTTP/SSE.

The naoqi_bridge service (Python 2.7 + qi SDK) handles the actual NAO connection.
This node translates ROS2 messages to/from the bridge's HTTP REST + SSE API.

Published topics (NAO → ROS2):
  /naoqi/speech_recognized  std_msgs/String
  /naoqi/sonar              sensor_msgs/Range
  /naoqi/battery            std_msgs/Float32
  /naoqi/bumper             std_msgs/Bool

Subscribed topics (ROS2 → NAO):
  /naoqi/say        std_msgs/String        → POST /say
  /naoqi/motion     std_msgs/String (JSON) → POST /motion
  /naoqi/leds       std_msgs/String (JSON) → POST /leds
  /naoqi/posture    std_msgs/String        → POST /posture
  /naoqi/move       std_msgs/String (JSON) → POST /move

Environment variables:
  NAOQI_BRIDGE_URL   URL of the naoqi_bridge service, default http://localhost:8090
  ROS_DOMAIN_ID      ROS2 domain (set consistently across all nao-brain nodes)
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

BRIDGE_URL = os.environ.get("NAOQI_BRIDGE_URL", "http://localhost:8090")
SSE_RECONNECT_DELAY = 5  # seconds before retrying SSE connection


class NAOqiDriverNode(Node):

    def __init__(self):
        super().__init__("naoqi_driver2")

        # ── Publishers: events from NAO ────────────────────────────────────────
        self.pub_speech = self.create_publisher(String, "/naoqi/speech_recognized", 10)
        self.pub_battery = self.create_publisher(Float32, "/naoqi/battery", 10)
        self.pub_bumper = self.create_publisher(Bool, "/naoqi/bumper", 10)
        self.pub_sonar = self.create_publisher(Range, "/naoqi/sonar", 10)

        # ── Subscribers: commands to NAO ───────────────────────────────────────
        self.create_subscription(String, "/naoqi/say", self._on_say, 10)
        self.create_subscription(String, "/naoqi/motion", self._on_motion, 10)
        self.create_subscription(String, "/naoqi/leds", self._on_leds, 10)
        self.create_subscription(String, "/naoqi/posture", self._on_posture, 10)
        self.create_subscription(String, "/naoqi/move", self._on_move, 10)

        # ── SSE listener (runs in background thread) ───────────────────────────
        self._sse_running = True
        self._sse_thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._sse_thread.start()

        self.get_logger().info(f"naoqi_driver2 started — bridge: {BRIDGE_URL}")

    # ──────────────────────────────────────────────────────────────────────────
    # SSE event stream from naoqi_bridge
    # ──────────────────────────────────────────────────────────────────────────

    def _sse_loop(self):
        """Continuously connect to the SSE endpoint and dispatch events."""
        while self._sse_running:
            try:
                with requests.get(f"{BRIDGE_URL}/events", stream=True, timeout=None) as resp:
                    resp.raise_for_status()
                    self.get_logger().info("SSE stream connected")
                    for raw_line in resp.iter_lines():
                        if not self._sse_running:
                            break
                        if raw_line and raw_line.startswith(b"data:"):
                            payload = raw_line[5:].strip()
                            if payload:
                                self._dispatch_event(json.loads(payload))
            except Exception as e:
                if self._sse_running:
                    self.get_logger().warn(f"SSE disconnected: {e} — retrying in {SSE_RECONNECT_DELAY}s")
                    time.sleep(SSE_RECONNECT_DELAY)

    def _dispatch_event(self, event: dict):
        topic = event.get("topic", "")

        if topic == "speech_recognized":
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
            msg.header.frame_id = "sonar_" + event.get("sensor", "front")
            msg.radiation_type = Range.ULTRASOUND
            msg.field_of_view = 0.523  # ~30 degrees in radians
            msg.min_range = 0.20
            msg.max_range = 2.55
            msg.range = float(event.get("data", 2.55))
            self.pub_sonar.publish(msg)

    # ──────────────────────────────────────────────────────────────────────────
    # ROS2 → NAO command handlers
    # ──────────────────────────────────────────────────────────────────────────

    def _post(self, endpoint: str, payload: dict):
        try:
            resp = requests.post(f"{BRIDGE_URL}{endpoint}", json=payload, timeout=10)
            if not resp.ok:
                self.get_logger().error(f"Bridge {endpoint} error {resp.status_code}: {resp.text}")
        except requests.RequestException as e:
            self.get_logger().error(f"Bridge {endpoint} request failed: {e}")

    def _on_say(self, msg: String):
        self._post("/say", {"text": msg.data})

    def _on_motion(self, msg: String):
        try:
            self._post("/motion", json.loads(msg.data))
        except json.JSONDecodeError as e:
            self.get_logger().error(f"/naoqi/motion bad JSON: {e}")

    def _on_leds(self, msg: String):
        try:
            self._post("/leds", json.loads(msg.data))
        except json.JSONDecodeError as e:
            self.get_logger().error(f"/naoqi/leds bad JSON: {e}")

    def _on_posture(self, msg: String):
        self._post("/posture", {"posture": msg.data})

    def _on_move(self, msg: String):
        try:
            self._post("/move", json.loads(msg.data))
        except json.JSONDecodeError as e:
            self.get_logger().error(f"/naoqi/move bad JSON: {e}")

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
