"""motion_engine — main ROS2 node, 20 Hz.

Subscriptions:
  /motion_engine/command  (std_msgs/String, JSON)  ← emotion payload from FastAPI

Publications:
  /naoqi/motion           (std_msgs/String, JSON)  → naoqi_driver → NAO

Tick loop:
  1. If no command received yet → idle drift on upper-body joints
  2. Resolve base pose from valence / vertical_split / lateral_bias
  3. If |valence| < AMBIVALENCE_THRESHOLD → ambivalence resolver instead
  4. If within pattern duration  → apply motion pattern on specified joints
  5. After pattern duration      → hold base pose + background drift
  6. Rate-limit + clamp to URDF → publish multi-joint command
"""

from __future__ import annotations

import json
import time
from typing import Any

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from .nao_joints import (
    AMBIVALENCE_THRESHOLD,
    JOINT_GROUPS,
    TICK_RATE_HZ,
    JointName,
)
from .pose_resolver import arousal_to_speed, resolve_pose
from .ambivalence_resolver import resolve_ambivalence
from .pattern_generator import (
    PATTERN_NAMES,
    drift,
    oscillate,
    settle,
    startle,
    tremor,
)
from .constraint_validator import validate

_TOPIC_IN  = "/motion_engine/command"
_TOPIC_OUT = "/naoqi/motion"

_SETTLE_DURATION: float = 2.0   # seconds for ease-out cubic settle
_IDLE_JOINTS: list[JointName] = JOINT_GROUPS["head"] + JOINT_GROUPS["arms"]

# Minimum joint change before publishing (avoid flooding naoqi_driver)
_PUBLISH_THRESHOLD: float = 0.001   # rad


class MotionEngineNode(Node):

    def __init__(self) -> None:
        super().__init__("motion_engine")

        self._sub = self.create_subscription(
            String, _TOPIC_IN, self._on_command, 10
        )
        self._pub = self.create_publisher(String, _TOPIC_OUT, 10)
        self._timer = self.create_timer(1.0 / TICK_RATE_HZ, self._tick)

        # Monotonic clock reference so pattern times stay stable across restarts
        self._node_start: float = time.monotonic()

        # Current emotion command
        self._cmd: dict[str, Any] = {}
        self._cmd_time: float = 0.0
        self._pattern_start: float = 0.0

        # Snapshot of joint angles at the moment a new command arrived
        self._settle_start: dict[JointName, float] = {}

        # Last angles sent to the robot (used for rate-limiting and ambivalence)
        self._current_angles: dict[JointName, float] = {}

        self.get_logger().info(
            f"motion_engine démarré — {_TOPIC_IN} → {_TOPIC_OUT} @ {TICK_RATE_HZ:.0f} Hz"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Subscription callback
    # ─────────────────────────────────────────────────────────────────────────

    def _on_command(self, msg: String) -> None:
        try:
            payload: dict[str, Any] = json.loads(msg.data)
        except json.JSONDecodeError:
            self.get_logger().warning("motion_engine: JSON invalide ignoré")
            return

        # Capture current position as settle start before overwriting command
        self._settle_start = dict(self._current_angles)
        self._cmd          = payload
        self._cmd_time     = time.monotonic()
        self._pattern_start = self._cmd_time

    # ─────────────────────────────────────────────────────────────────────────
    # 20 Hz main loop
    # ─────────────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        now         = time.monotonic()
        t_global    = now - self._node_start
        t_since_cmd = now - self._cmd_time

        if not self._cmd:
            self._tick_idle(t_global)
            return

        self._tick_active(t_since_cmd, t_global)

    def _tick_idle(self, t_global: float) -> None:
        """No command yet — animate upper body with background drift."""
        deltas = drift(_IDLE_JOINTS, t_global)
        target = {
            j: self._current_angles.get(j, 0.0) + deltas.get(j, 0.0)
            for j in _IDLE_JOINTS
        }
        self._publish_and_update(target, speed=0.05)

    def _tick_active(self, t_since_cmd: float, t_global: float) -> None:
        arousal  = float(self._cmd.get("arousal",        0.5))
        valence  = float(self._cmd.get("valence",        0.0))
        vsplit   = float(self._cmd.get("vertical_split", 0.0))
        lbias    = float(self._cmd.get("lateral_bias",   0.0))
        motion   = self._cmd.get("motion", {})

        speed = arousal_to_speed(arousal)

        # ── Step 1: resolve base pose ─────────────────────────────────────
        base_pose = resolve_pose(valence, vsplit, lbias)

        if abs(valence) < AMBIVALENCE_THRESHOLD:
            base_pose = resolve_ambivalence(self._current_angles, base_pose)

        # ── Step 2: apply motion pattern ─────────────────────────────────
        pattern_name   = motion.get("pattern", "settle")
        pattern_joints = motion.get("joints", [])
        intensity      = float(motion.get("intensity", 0.5))
        cycles         = int(motion.get("cycles", 2))

        if pattern_name not in PATTERN_NAMES:
            self.get_logger().warning(
                f"motion_engine: pattern inconnu '{pattern_name}' → settle"
            )
            pattern_name = "settle"

        target = dict(base_pose)

        if pattern_name == "oscillate":
            frequency = max(arousal * 3.0, 0.1)
            duration  = cycles / frequency

            if t_since_cmd < duration:
                deltas = oscillate(
                    pattern_joints, t_since_cmd, arousal, intensity, cycles
                )
                for j, d in deltas.items():
                    target[j] = base_pose.get(j, 0.0) + d
            else:
                # Pattern finished — hold pose + background drift
                self._apply_background_drift(target, t_global)

        elif pattern_name == "settle":
            joints_to_settle = pattern_joints if pattern_joints else list(base_pose)

            if t_since_cmd < _SETTLE_DURATION:
                interpolated = settle(
                    joints_to_settle,
                    t_since_cmd,
                    _SETTLE_DURATION,
                    self._settle_start,
                    base_pose,
                )
                target.update(interpolated)
            # After settle completes, target == base_pose (already set above)

        elif pattern_name == "drift":
            joints_to_drift = pattern_joints if pattern_joints else _IDLE_JOINTS
            deltas = drift(joints_to_drift, t_global)
            for j, d in deltas.items():
                target[j] = base_pose.get(j, 0.0) + d

        elif pattern_name == "startle":
            frequency = max(arousal * 3.0, 0.5)
            duration  = cycles / frequency

            if t_since_cmd < duration:
                deltas = startle(pattern_joints, t_since_cmd, intensity, duration)
                for j, d in deltas.items():
                    target[j] = base_pose.get(j, 0.0) + d
            else:
                self._apply_background_drift(target, t_global)

        elif pattern_name == "tremor":
            joints_tremor = pattern_joints if pattern_joints else _IDLE_JOINTS
            deltas = tremor(joints_tremor, t_global)
            for j, d in deltas.items():
                target[j] = base_pose.get(j, 0.0) + d

        self._publish_and_update(target, speed)

    def _apply_background_drift(
        self, target: dict[JointName, float], t_global: float
    ) -> None:
        """Layer idle drift on upper-body joints of an existing target dict."""
        deltas = drift(_IDLE_JOINTS, t_global)
        for j, d in deltas.items():
            if j in target:
                target[j] = target[j] + d

    # ─────────────────────────────────────────────────────────────────────────
    # Validate, track state, and publish
    # ─────────────────────────────────────────────────────────────────────────

    def _publish_and_update(
        self,
        target: dict[JointName, float],
        speed: float,
    ) -> None:
        validated = validate(self._current_angles, target)

        # Filter joints that moved significantly to reduce bus noise
        joints = [
            j for j, a in validated.items()
            if abs(a - self._current_angles.get(j, a)) >= _PUBLISH_THRESHOLD
        ]

        if joints:
            angles  = [validated[j] for j in joints]
            payload = json.dumps({
                "joints": joints,
                "angles": [round(a, 5) for a in angles],
                "speed":  round(speed, 4),
            })
            msg      = String()
            msg.data = payload
            self._pub.publish(msg)

        self._current_angles.update(validated)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = MotionEngineNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()
