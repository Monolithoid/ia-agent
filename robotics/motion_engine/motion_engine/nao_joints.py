"""NAO V5 URDF joint limits, biomechanical pose targets, and motion constants.

All hardcoded angle values live here — nowhere else in the package.
"""

from __future__ import annotations

from typing import TypeAlias

JointName: TypeAlias = str

# ─────────────────────────────────────────────────────────────────────────────
# URDF joint limits  (min_rad, max_rad)
# Source: NAO V5 technical reference / Softbank Robotics URDF
# ─────────────────────────────────────────────────────────────────────────────

JOINT_LIMITS: dict[JointName, tuple[float, float]] = {
    # Head
    "HeadYaw":        (-2.0857,  2.0857),
    "HeadPitch":      (-0.6720,  0.5149),
    # Left arm
    "LShoulderPitch": (-2.0857,  2.0857),
    "LShoulderRoll":  (-0.3142,  1.3265),
    "LElbowYaw":      (-2.0857,  2.0857),
    "LElbowRoll":     (-1.5446, -0.0349),   # always ≤ 0
    "LWristYaw":      (-1.8238,  1.8238),
    # Right arm
    "RShoulderPitch": (-2.0857,  2.0857),
    "RShoulderRoll":  (-1.3265,  0.3142),
    "RElbowYaw":      (-2.0857,  2.0857),
    "RElbowRoll":     ( 0.0349,  1.5446),   # always ≥ 0
    "RWristYaw":      (-1.8238,  1.8238),
    # Left leg
    "LHipYawPitch":   (-1.1453,  0.7408),
    "LHipRoll":       (-0.3794,  0.7904),
    "LHipPitch":      (-1.5359,  0.4840),
    "LKneePitch":     (-0.0923,  2.1125),
    "LAnklePitch":    (-1.1903,  0.9227),
    "LAnkleRoll":     (-0.3978,  0.7690),
    # Right leg
    "RHipYawPitch":   (-1.1453,  0.7408),
    "RHipRoll":       (-0.7904,  0.3794),
    "RHipPitch":      (-1.5359,  0.4840),
    "RKneePitch":     (-0.0923,  2.1125),
    "RAnklePitch":    (-1.1903,  0.9227),
    "RAnkleRoll":     (-0.7690,  0.3978),
}

# Pre-computed range sizes — used by pattern_generator for amplitude scaling
JOINT_RANGES: dict[JointName, float] = {
    name: hi - lo for name, (lo, hi) in JOINT_LIMITS.items()
}

# ─────────────────────────────────────────────────────────────────────────────
# Topological joint groups
# ─────────────────────────────────────────────────────────────────────────────

JOINT_GROUPS: dict[str, list[JointName]] = {
    "head":      ["HeadYaw", "HeadPitch"],
    "left_arm":  ["LShoulderPitch", "LShoulderRoll", "LElbowYaw", "LElbowRoll", "LWristYaw"],
    "right_arm": ["RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll", "RWristYaw"],
    "left_leg":  ["LHipYawPitch", "LHipRoll", "LHipPitch", "LKneePitch", "LAnklePitch", "LAnkleRoll"],
    "right_leg": ["RHipYawPitch", "RHipRoll", "RHipPitch", "RKneePitch", "RAnklePitch", "RAnkleRoll"],
}
JOINT_GROUPS["arms"]  = JOINT_GROUPS["left_arm"]  + JOINT_GROUPS["right_arm"]
JOINT_GROUPS["legs"]  = JOINT_GROUPS["left_leg"]  + JOINT_GROUPS["right_leg"]
JOINT_GROUPS["upper"] = JOINT_GROUPS["head"]       + JOINT_GROUPS["arms"]
JOINT_GROUPS["lower"] = JOINT_GROUPS["legs"]
JOINT_GROUPS["left"]  = JOINT_GROUPS["head"] + JOINT_GROUPS["left_arm"]  + JOINT_GROUPS["left_leg"]
JOINT_GROUPS["right"] = JOINT_GROUPS["head"] + JOINT_GROUPS["right_arm"] + JOINT_GROUPS["right_leg"]
JOINT_GROUPS["all"]   = list(JOINT_LIMITS.keys())

# ─────────────────────────────────────────────────────────────────────────────
# Biomechanical pose targets
#
# CLOSED (valence → -1): phylogenetic protection reflex
#   • elevated shoulders, internal arm rotation, elbows toward torso,
#     head bowed, hips slightly inward
#
# OPEN   (valence → +1): engagement / confidence posture
#   • shoulders down and externally rotated, arms extended, head level,
#     hips slightly wider
#
# pose_resolver linearly interpolates between these two poles.
# ─────────────────────────────────────────────────────────────────────────────

CLOSED_POSE: dict[JointName, float] = {
    "HeadYaw":        0.00,
    "HeadPitch":      0.40,    # head bowed — positive = downward on NAO
    "LShoulderPitch": 1.50,    # arms angled downward/forward
    "LShoulderRoll":  0.08,    # arms close to torso (internal)
    "LElbowYaw":     -0.50,    # internal rotation
    "LElbowRoll":    -1.20,    # elbow strongly bent toward torso
    "LWristYaw":      0.00,
    "RShoulderPitch": 1.50,
    "RShoulderRoll":  -0.08,   # mirrored
    "RElbowYaw":      0.50,
    "RElbowRoll":     1.20,
    "RWristYaw":      0.00,
    "LHipYawPitch":  -0.10,    # hips slightly inward (guard stance)
    "LHipRoll":      -0.05,
    "LHipPitch":     -0.20,
    "LKneePitch":     0.35,    # slight knee flexion
    "LAnklePitch":   -0.15,
    "LAnkleRoll":     0.00,
    "RHipYawPitch":  -0.10,
    "RHipRoll":       0.05,    # mirrored
    "RHipPitch":     -0.20,
    "RKneePitch":     0.35,
    "RAnklePitch":   -0.15,
    "RAnkleRoll":     0.00,
}

OPEN_POSE: dict[JointName, float] = {
    "HeadYaw":        0.00,
    "HeadPitch":     -0.10,    # slightly upward — alert / engaged
    "LShoulderPitch": 0.90,    # arms more forward/up
    "LShoulderRoll":  0.40,    # arms away from body (external rotation)
    "LElbowYaw":     -1.40,    # external rotation
    "LElbowRoll":    -0.25,    # arms more extended
    "LWristYaw":      0.00,
    "RShoulderPitch": 0.90,
    "RShoulderRoll":  -0.40,   # mirrored
    "RElbowYaw":      1.40,
    "RElbowRoll":     0.25,
    "RWristYaw":      0.00,
    "LHipYawPitch":   0.05,    # hips very slightly wider
    "LHipRoll":       0.08,
    "LHipPitch":     -0.05,
    "LKneePitch":     0.10,    # nearly straight
    "LAnklePitch":    0.00,
    "LAnkleRoll":     0.00,
    "RHipYawPitch":   0.05,
    "RHipRoll":      -0.08,    # mirrored
    "RHipPitch":     -0.05,
    "RKneePitch":     0.10,
    "RAnklePitch":    0.00,
    "RAnkleRoll":     0.00,
}

# ─────────────────────────────────────────────────────────────────────────────
# Motion constants
# ─────────────────────────────────────────────────────────────────────────────

# Arousal → NAO joint speed mapping  (rad/s)
SPEED_MIN: float = 0.05
SPEED_MAX: float = 1.00

# |valence| below this → ambivalence resolution instead of interpolation
AMBIVALENCE_THRESHOLD: float = 0.3

# Constraint: maximum joint delta per 100 ms
MAX_DELTA_PER_100MS: float = 0.5   # rad

# Node tick rate
TICK_RATE_HZ: float = 20.0

# Derived: max delta per tick at TICK_RATE_HZ
MAX_DELTA_PER_TICK: float = MAX_DELTA_PER_100MS * (1.0 / TICK_RATE_HZ) / 0.1
