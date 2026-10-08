"""Maps (valence, vertical_split, lateral_bias) → per-joint target angles.

Interpolation poles:
  valence = -1 → CLOSED_POSE  (phylogenetic protection / closure)
  valence = +1 → OPEN_POSE    (engagement / openness)

vertical_split dissociates upper and lower body:
  +1 → upper body uses (valence + 1) clamped, lower body uses (valence - 1) clamped
  -1 → inverse

lateral_bias rotates head toward a stimulus and slightly shifts arm weight.
"""

from __future__ import annotations

import numpy as np

from .nao_joints import (
    JOINT_LIMITS,
    JOINT_GROUPS,
    CLOSED_POSE,
    OPEN_POSE,
    SPEED_MIN,
    SPEED_MAX,
    JointName,
)

_UPPER_JOINTS: frozenset[JointName] = frozenset(JOINT_GROUPS["upper"])
_LOWER_JOINTS: frozenset[JointName] = frozenset(JOINT_GROUPS["lower"])


def resolve_pose(
    valence: float,
    vertical_split: float,
    lateral_bias: float,
) -> dict[JointName, float]:
    """Return absolute target angles for all joints from emotional parameters.

    All three inputs are clamped to [-1, 1] so callers need not sanitise them.
    vertical_split shifts effective valence per body half so upper and lower
    body can express different emotional intensities simultaneously.
    """
    valence        = float(np.clip(valence,        -1.0, 1.0))
    vertical_split = float(np.clip(vertical_split, -1.0, 1.0))
    lateral_bias   = float(np.clip(lateral_bias,   -1.0, 1.0))

    # Shift valence up/down for each body half according to the split parameter.
    # Clamping after addition keeps both values in the [-1, 1] interpolation domain.
    upper_valence = float(np.clip(valence + vertical_split, -1.0, 1.0))
    lower_valence = float(np.clip(valence - vertical_split, -1.0, 1.0))

    pose: dict[JointName, float] = {}

    for joint in JOINT_GROUPS["all"]:
        # Skip joints that have no target in either biomechanical pole —
        # interpolating between a known value and a missing one is undefined.
        if joint not in CLOSED_POSE or joint not in OPEN_POSE:
            continue

        # Apply upper-body or lower-body valence depending on which half this joint belongs to.
        eff_valence = upper_valence if joint in _UPPER_JOINTS else lower_valence

        # Linear interpolation: t=0 → CLOSED (protection), t=1 → OPEN (engagement).
        t = (eff_valence + 1.0) * 0.5
        pose[joint] = CLOSED_POSE[joint] * (1.0 - t) + OPEN_POSE[joint] * t

    # Lateral bias: turn head toward the detected stimulus side.
    # HeadYaw is the only head joint affected; the others stay on the valence axis.
    if "HeadYaw" in pose:
        head_yaw_max = JOINT_LIMITS["HeadYaw"][1]
        pose["HeadYaw"] = float(np.clip(
            lateral_bias * head_yaw_max,
            JOINT_LIMITS["HeadYaw"][0],
            JOINT_LIMITS["HeadYaw"][1],
        ))

    # Subtle arm-weight shift: ipsilateral shoulder rolls outward slightly
    # (body orients toward the detected stimulus side)
    _BIAS_ROLL_AMPLITUDE = 0.12  # rad — intentionally small
    for joint, sign in (("LShoulderRoll", 1.0), ("RShoulderRoll", -1.0)):
        # Only adjust shoulders that were actually computed in the pose loop above.
        if joint in pose:
            lo, hi = JOINT_LIMITS[joint]
            pose[joint] = float(np.clip(
                pose[joint] + sign * lateral_bias * _BIAS_ROLL_AMPLITUDE,
                lo, hi,
            ))

    return pose


def arousal_to_speed(arousal: float) -> float:
    """Map arousal [0, 1] → joint speed [SPEED_MIN, SPEED_MAX] rad/s.

    Higher arousal produces faster motion, reflecting higher energy states.
    Input is clamped so callers need not pre-sanitise.
    """
    arousal = float(np.clip(arousal, 0.0, 1.0))
    return SPEED_MIN + arousal * (SPEED_MAX - SPEED_MIN)
