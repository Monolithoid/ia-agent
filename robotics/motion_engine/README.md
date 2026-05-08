# motion_engine

ROS2 Humble package — emotion-driven biomechanical motion synthesis for NAO V5.

## Architecture

```
/motion_engine/command  (std_msgs/String, JSON)
        ↓
  MotionEngineNode  @  20 Hz
        ↓
/naoqi/motion  (std_msgs/String, JSON)
        ↓
  naoqi_driver  →  NAO WebSocket bridge  →  ALMotion
```

## Input format

```json
{
  "arousal":        0.85,
  "valence":        0.90,
  "vertical_split": 0.20,
  "lateral_bias":   0.10,
  "motion": {
    "pattern":   "oscillate",
    "joints":    ["RElbowRoll", "RShoulderPitch"],
    "intensity": 0.80,
    "cycles":    4
  }
}
```

| Field | Range | Effect |
|---|---|---|
| `arousal` | [0, 1] | joint speed 0.05 → 1.0 rad/s |
| `valence` | [-1, 1] | pose interpolation CLOSED↔OPEN |
| `vertical_split` | [-1, 1] | dissociates upper/lower body valence |
| `lateral_bias` | [-1, 1] | head yaw + shoulder roll asymmetry |
| `motion.pattern` | see below | temporal motion pattern |

### Motion patterns

| Pattern | Description |
|---|---|
| `oscillate` | sin wave, frequency = arousal × 3 Hz, Gaussian bell envelope |
| `settle` | ease-out cubic interpolation to base pose (2 s) |
| `drift` | 0.3 Hz idle breathing, ±0.05 rad |
| `startle` | spike (10 % duration) + damped exponential return |
| `tremor` | 8 Hz fine trembling, ±0.02 rad |

## Pose model

**CLOSED** (valence = −1): protection of vital organs — shoulders elevated and internally rotated, elbows bent toward torso, head bowed, hips inward.

**OPEN** (valence = +1): engagement / confidence — shoulders down and externally rotated, arms extended, head level, hips slightly wider.

**Ambivalence** (|valence| < 0.3): instead of averaging, reads current joint position and pushes each joint toward its already-initiated direction. No randomness — produces consistent personality over time.

## Integration with body_gateway

body_gateway automatically maps categorical emotions to `/motion_engine/command`:

| Emotion | arousal | valence | pattern |
|---|---|---|---|
| curious | 0.55 | +0.35 | drift (head) |
| excited | 0.85 | +0.80 | oscillate (shoulders) |
| neutral | 0.30 | 0.00 | settle |
| uncertain | 0.45 | −0.25 | tremor (head) |

Full payloads can also be injected directly:

```bash
curl -X POST http://localhost:8095/motion-command \
     -H 'Content-Type: application/json' \
     -d '{"arousal":0.85,"valence":0.9,"vertical_split":0.2,"lateral_bias":0.1,
          "motion":{"pattern":"oscillate","joints":["RElbowRoll"],"intensity":0.8,"cycles":4}}'
```

## Build

```bash
cd /path/to/ros2_ws
colcon build --packages-select motion_engine
source install/setup.bash
ros2 run motion_engine motion_engine
```

## Constraints

- All joint angle targets defined exclusively in `nao_joints.py`
- No `random()` anywhere — identical inputs produce identical outputs
- Rate limit: 0.5 rad / 100 ms per joint
- URDF hard clamp applied every tick
- Python 3.10+, ROS2 Humble
