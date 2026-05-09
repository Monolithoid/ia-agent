"""Joint constraint enforcement: URDF limit clamping + delta rate limiting.

Two-stage pipeline applied every tick before publishing:
  1. rate_limit  — clamp per-tick delta to MAX_DELTA_PER_TICK (0.5 rad / 100 ms)
  2. clamp       — hard clamp to URDF [min, max] range

Always call validate(); it applies both stages in the correct order.
"""

from __future__ import annotations

import numpy as np

from .nao_joints import JOINT_LIMITS, MAX_DELTA_PER_TICK, JointName


def clamp_to_limits(angles: dict[JointName, float]) -> dict[JointName, float]:
    """Hard clamp each joint to its URDF range.

    Joints not in JOINT_LIMITS (e.g. not on this robot model) are passed
    through unchanged — clamping an unknown joint could corrupt its value.
    """
    result: dict[JointName, float] = {}
    for joint, angle in angles.items():
        if joint in JOINT_LIMITS:
            # Joint has a defined URDF range — clamp to protect the hardware.
            lo, hi = JOINT_LIMITS[joint]
            result[joint] = float(np.clip(angle, lo, hi))
        else:
            # Unknown joint — pass through without modification.
            result[joint] = angle
    return result


def apply_rate_limit(
    current: dict[JointName, float],
    target: dict[JointName, float],
) -> dict[JointName, float]:
    """Limit per-tick angular displacement to MAX_DELTA_PER_TICK.

    Prevents instantaneous jumps that would feel mechanical or damage gears.
    Joints absent from current are assumed already at target (no delta).
    """
    result: dict[JointName, float] = {}
    for joint, target_angle in target.items():
        current_angle = current.get(joint, target_angle)
        delta = float(np.clip(
            target_angle - current_angle,
            -MAX_DELTA_PER_TICK,
             MAX_DELTA_PER_TICK,
        ))
        result[joint] = current_angle + delta
    return result


def validate(
    current: dict[JointName, float],
    target: dict[JointName, float],
) -> dict[JointName, float]:
    """Rate-limit then clamp to URDF limits. Always use this entry point.

    Order matters: rate-limit first (shape the trajectory), then clamp
    (ensure physical safety). Reversing the order would allow rate-limiter
    to produce values outside URDF bounds after the clamp step.
    """
    return clamp_to_limits(apply_rate_limit(current, target))
