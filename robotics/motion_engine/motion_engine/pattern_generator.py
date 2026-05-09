"""Deterministic temporal motion patterns.

All five patterns are pure functions of time — no random(), no global state.
At identical inputs they always produce identical outputs.

Delta-returning patterns (add to base pose):
  oscillate  — sinusoidal with bell-curve amplitude envelope
  drift      — slow 0.3 Hz idle breathing oscillation
  startle    — sharp spike + damped exponential decay
  tremor     — high-frequency fine-motor trembling

Absolute-returning patterns (replace base pose for specified joints):
  settle     — ease-out cubic interpolation from start to target
"""

from __future__ import annotations

import math

from .nao_joints import JOINT_RANGES, JointName

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bell(t_norm: float, sigma: float = 0.18) -> float:
    """Gaussian bell envelope over normalised time [0, 1], peak at 0.5.

    Used to taper oscillation amplitude so it ramps up and fades out naturally
    instead of starting/stopping abruptly.
    """
    return math.exp(-((t_norm - 0.5) ** 2) / (2.0 * sigma ** 2))


def _ease_out_cubic(t_norm: float) -> float:
    """Ease-out cubic: slow finish. f(0)=0, f(1)=1.

    Produces natural deceleration toward the target pose,
    mirroring the way biological muscles resist overshoot.
    """
    return 1.0 - (1.0 - min(t_norm, 1.0)) ** 3


# ─────────────────────────────────────────────────────────────────────────────
# Public pattern functions
# ─────────────────────────────────────────────────────────────────────────────

def oscillate(
    joints: list[JointName],
    t: float,
    arousal: float,
    intensity: float,
    cycles: int,
) -> dict[JointName, float]:
    """Sinusoidal oscillation with Gaussian bell-curve amplitude envelope.

    frequency  = arousal * 3.0 Hz
    amplitude  = intensity * joint_range * 0.3
    duration   = cycles / frequency
    envelope   = Gaussian bell peaking at mid-duration
    """
    # Floor frequency at 0.1 Hz to avoid division by zero when arousal is 0.
    frequency = max(arousal * 3.0, 0.1)
    duration  = cycles / frequency
    t_norm    = t / duration if duration > 0 else 1.0
    envelope  = _bell(min(t_norm, 1.0))

    result: dict[JointName, float] = {}
    for joint in joints:
        amplitude = intensity * JOINT_RANGES.get(joint, 1.0) * 0.3
        result[joint] = amplitude * math.sin(2.0 * math.pi * frequency * t) * envelope
    return result


def settle(
    joints: list[JointName],
    t: float,
    duration: float,
    start_angles: dict[JointName, float],
    target_angles: dict[JointName, float],
) -> dict[JointName, float]:
    """Ease-out cubic interpolation from start_angles to target_angles.

    Returns absolute angles (not deltas).
    Joints missing from start_angles default to their target value (no travel).
    """
    alpha = _ease_out_cubic(t / duration if duration > 0 else 1.0)
    result: dict[JointName, float] = {}
    for joint in joints:
        s = start_angles.get(joint, target_angles.get(joint, 0.0))
        g = target_angles.get(joint, s)
        result[joint] = s + alpha * (g - s)
    return result


def drift(
    joints: list[JointName],
    t: float,
) -> dict[JointName, float]:
    """Slow sinusoidal idle oscillation (simulates passive breathing).

    0.3 Hz, ±0.05 rad — always running; safe to layer on top of any pose.
    """
    _FREQ = 0.3    # Hz
    _AMP  = 0.05   # rad
    val   = _AMP * math.sin(2.0 * math.pi * _FREQ * t)
    return {joint: val for joint in joints}


def startle(
    joints: list[JointName],
    t: float,
    intensity: float,
    duration: float,
) -> dict[JointName, float]:
    """Rapid spike (first 10 % of duration) then damped exponential return.

    amplitude = intensity * joint_range * 0.2
    The spike mimics the human startle reflex: fast onset, slower recovery.
    """
    # Guard: zero/negative duration has no physical meaning — return neutral.
    if duration <= 0.0:
        return {j: 0.0 for j in joints}

    peak_t = 0.1 * duration
    tau    = 0.9 * duration / 3.0     # time constant for exponential decay

    result: dict[JointName, float] = {}
    for joint in joints:
        amplitude = intensity * JOINT_RANGES.get(joint, 1.0) * 0.2
        if t <= peak_t:
            # Rising phase: linear ramp from 0 to peak amplitude.
            delta = amplitude * (t / peak_t)
        else:
            # Decay phase: exponential return toward 0 after the peak.
            delta = amplitude * math.exp(-(t - peak_t) / max(tau, 1e-6))
        result[joint] = delta
    return result


def tremor(
    joints: list[JointName],
    t: float,
) -> dict[JointName, float]:
    """High-frequency fine motor trembling.

    8 Hz, ±0.02 rad — mimics uncertainty or cold-stimulus micro-vibration.
    """
    _FREQ = 8.0    # Hz
    _AMP  = 0.02   # rad
    val   = _AMP * math.sin(2.0 * math.pi * _FREQ * t)
    return {joint: val for joint in joints}


PATTERN_NAMES: frozenset[str] = frozenset(
    {"oscillate", "settle", "drift", "startle", "tremor"}
)
