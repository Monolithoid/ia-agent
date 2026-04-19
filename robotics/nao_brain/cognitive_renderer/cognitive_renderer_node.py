#!/usr/bin/env python3
"""
CognitiveRenderer — ROS2 Humble node translating cognitive/emotional states
into continuous NAO physical expression (LEDs + head motion).

NAO is ALWAYS alive: animations run continuously even during the 3-8 s Qwen3
inference window. The node runs an internal animation loop at 3 Hz.

Subscribed topics:
  /cognitive_state   std_msgs/String  (thinking|responding|listening|dreaming|
                                       idle|uncertain|excited)
  /dream_state       std_msgs/String  (phase name from DreamEngine)
  /emotion_state     std_msgs/String  (curious|neutral|excited|uncertain)

Published topics (forwarded to naoqi_driver):
  /naoqi/leds        std_msgs/String  JSON {group, r, g, b, duration}
  /naoqi/motion      std_msgs/String  JSON {joint, angle, speed}
"""

import json
import math
import threading
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

# ──────────────────────────────────────────────────────────────────────────────
# State → rendering profile
# ──────────────────────────────────────────────────────────────────────────────

_PROFILES = {
    # cognitive state → (r, g, b, pulse, head_mode)
    "thinking":   (0.00, 0.00, 1.00, True,  "tilt_down"),
    "responding": (1.00, 1.00, 1.00, False, "straight"),
    "listening":  (0.00, 0.80, 0.20, False, "slight_forward"),
    "dreaming":   (0.50, 0.00, 0.50, True,  "micro_yaw"),
    "idle":       (0.05, 0.05, 0.10, False, "drift"),
    "uncertain":  (0.80, 0.60, 0.00, True,  "slow_yaw"),
    "excited":    (1.00, 1.00, 1.00, False, "look_up"),
}

_EMOTION_TO_STATE = {
    "curious":   "thinking",
    "excited":   "excited",
    "uncertain": "uncertain",
    "neutral":   "idle",
}

_ANIM_PERIOD = 0.33  # seconds per animation tick (~3 Hz)


class CognitiveRendererNode(Node):

    def __init__(self):
        super().__init__("cognitive_renderer")

        self._state = "idle"
        self._lock = threading.Lock()
        self._running = True
        self._phase = 0.0  # animation phase accumulator

        # ── Subscribers ────────────────────────────────────────────────────────
        self.create_subscription(String, "/cognitive_state", self._on_cognitive, 10)
        self.create_subscription(String, "/dream_state", self._on_dream, 10)
        self.create_subscription(String, "/emotion_state", self._on_emotion, 10)

        # ── Publishers ─────────────────────────────────────────────────────────
        self.pub_leds = self.create_publisher(String, "/naoqi/leds", 10)
        self.pub_motion = self.create_publisher(String, "/naoqi/motion", 10)

        # ── Animation loop (runs in background thread) ─────────────────────────
        self._anim_thread = threading.Thread(target=self._animation_loop, daemon=True)
        self._anim_thread.start()

        self.get_logger().info("CognitiveRenderer started")

    # ──────────────────────────────────────────────────────────────────────────
    # State updates
    # ──────────────────────────────────────────────────────────────────────────

    def _on_cognitive(self, msg: String):
        state = msg.data.lower().strip()
        if state in _PROFILES:
            with self._lock:
                self._state = state
            self.get_logger().info(f"Cognitive state → {state}")

    def _on_dream(self, msg: String):
        with self._lock:
            self._state = "dreaming"
        self.get_logger().info(f"Dream phase: {msg.data}")

    def _on_emotion(self, msg: String):
        emotion = msg.data.lower().strip()
        mapped = _EMOTION_TO_STATE.get(emotion)
        if mapped:
            with self._lock:
                # Emotion overrides only when robot is in neutral idle state
                if self._state == "idle":
                    self._state = mapped

    # ──────────────────────────────────────────────────────────────────────────
    # Animation loop
    # ──────────────────────────────────────────────────────────────────────────

    def _animation_loop(self):
        while self._running:
            with self._lock:
                state = self._state

            profile = _PROFILES.get(state, _PROFILES["idle"])
            r, g, b, pulse, head_mode = profile

            self._render_leds(r, g, b, pulse)
            self._render_head(head_mode)

            self._phase += _ANIM_PERIOD
            time.sleep(_ANIM_PERIOD)

    def _render_leds(self, r: float, g: float, b: float, pulse: bool):
        if pulse:
            factor = 0.5 + 0.5 * math.sin(self._phase * 1.5)
            r *= factor
            g *= factor
            b *= factor

        msg = String()
        msg.data = json.dumps({"group": "AllLeds", "r": round(r, 3),
                                "g": round(g, 3), "b": round(b, 3), "duration": 0.3})
        self.pub_leds.publish(msg)

    def _render_head(self, head_mode: str):
        """Publish a head joint command based on current mode and animation phase."""
        msg = String()

        if head_mode == "tilt_down":
            angle = -0.20 + 0.08 * math.sin(self._phase * 0.4)
            msg.data = json.dumps({"joint": "HeadPitch", "angle": round(angle, 3), "speed": 0.04})

        elif head_mode == "straight":
            msg.data = json.dumps({"joint": "HeadPitch", "angle": 0.0, "speed": 0.12})

        elif head_mode == "slight_forward":
            msg.data = json.dumps({"joint": "HeadPitch", "angle": 0.10, "speed": 0.08})

        elif head_mode == "micro_yaw":
            angle = 0.06 * math.sin(self._phase * 0.15)
            msg.data = json.dumps({"joint": "HeadYaw", "angle": round(angle, 3), "speed": 0.02})

        elif head_mode == "drift":
            # Gentle idle breathing — very slow yaw oscillation
            angle = 0.12 * math.sin(self._phase * 0.07)
            msg.data = json.dumps({"joint": "HeadYaw", "angle": round(angle, 3), "speed": 0.02})

        elif head_mode == "slow_yaw":
            angle = 0.18 * math.sin(self._phase * 0.25)
            msg.data = json.dumps({"joint": "HeadYaw", "angle": round(angle, 3), "speed": 0.04})

        elif head_mode == "look_up":
            msg.data = json.dumps({"joint": "HeadPitch", "angle": -0.30, "speed": 0.15})

        else:
            return

        self.pub_motion.publish(msg)

    def destroy_node(self):
        self._running = False
        super().destroy_node()


def main():
    rclpy.init()
    node = CognitiveRendererNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
