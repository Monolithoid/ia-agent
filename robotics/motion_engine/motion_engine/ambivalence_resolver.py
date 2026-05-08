"""Behavioural-continuity resolver for ambivalent emotional states.

Called when |valence| < AMBIVALENCE_THRESHOLD.  Instead of computing a
mid-way average pose (which reads as expressionless), we look at where each
joint already is and push it toward its already-initiated direction:

  current ≥ joint midpoint  →  push toward OPEN_POSE value
  current <  joint midpoint  →  push toward CLOSED_POSE value

This produces a personality that "commits" to what it has started, yielding
consistent character over time without any random component.
"""

from __future__ import annotations

from .nao_joints import JOINT_LIMITS, CLOSED_POSE, OPEN_POSE, JointName


def resolve_ambivalence(
    current_angles: dict[JointName, float],
    base_pose: dict[JointName, float],
) -> dict[JointName, float]:
    """Return a committed pose derived from current joint positions."""
    resolved: dict[JointName, float] = dict(base_pose)

    for joint, current in current_angles.items():
        if joint not in JOINT_LIMITS:
            continue
        lo, hi = JOINT_LIMITS[joint]
        midpoint = (lo + hi) * 0.5

        if current >= midpoint:
            # Already in the upper half → commit to openness
            resolved[joint] = OPEN_POSE.get(joint, base_pose.get(joint, current))
        else:
            # Already in the lower half → commit to closure
            resolved[joint] = CLOSED_POSE.get(joint, base_pose.get(joint, current))

    return resolved
