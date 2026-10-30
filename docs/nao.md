# Robot NAO

Documentation for the NAO robot bridge embedded on the robot and its integration with the VPS stack.

---

## Connection principle

The NAO cannot receive inbound connections from the internet (local network firewall, NAT). The chosen solution is a **Python 2.7 script embedded on the robot** that establishes an **outbound** WebSocket connection to rosbridge, through the static drone's relay ([ADR 006](decisions/006-nao-via-drone-statique.md)).

```
NAO (Wi-Fi, local network)
  └─► nao_ws_bridge.py (Python 2.7, no external dependencies)
        └─► Outbound WebSocket ws://DRONE_IP:9090 (local network, no token)
              └─► static drone relay (Caddy): wss://rosbridge.$DOMAIN + token
                    └─► rosbridge-proxy → ros2-bridge (ROS 2 Humble, VPS)
                    └─► nao-core (naoqi_driver + cognitive_renderer [+ motion_engine si MOTION_ENGINE=1])
                          └─► body_gateway → Ollama (LLM)
```

Once connected the bridge subscribes to command topics and executes them via the native NAOqi SDK. It also publishes status (battery) and captured audio back to the VPS.

> See [ADR 002](decisions/002-nao-websocket-bridge.md) for rejected alternatives (VPN, Raspberry Pi relay).

### Through the static drone (ADR 006)

The robot's Python 2.7 cannot do TLS, and the VPS no longer accepts rosbridge
over plain HTTP. The NAO stays on the home Wi-Fi and talks to a relay on the
static drone, which forwards to the VPS over TLS and adds the token. The local
network is considered safe.

The robot runs `--vps ws://DRONE_IP:9090/` **without** `--token`: the secret
only lives on the drone and the VPS. Setup:
[`robotics/drones/static/onboard/nao-relay/`](../robotics/drones/static/onboard/nao-relay/README.md).
For a NAO without a drone, see [SSL / WSS](#ssl--wss).

---

## Scripts overview

| Script | Runs on | Role |
|---|---|---|
| `robotics/drones/nao/onboard/nao_ws_bridge.py` | **NAO robot** | Outbound WebSocket client → rosbridge VPS |
| `robotics/drones/nao/bridge/app.py` | **VPS (Docker)** | HTTP compatibility API (fallback, robot-initiated mode) |
| `robotics/drones/nao/driver/naoqi_driver_node.py` | **VPS (container nao-core)** | Bridges `/bodies/*/actuators/*` ↔ `/nao/*` topics |
| `robotics/drones/nao/cognitive_renderer/cognitive_renderer_node.py` | **VPS (container nao-core)** | Translates cognitive state → LED + head animations |
| `robotics/brain/body_gateway/body_gateway.py` | **VPS (ROS 2)** | LLM (Ollama) ↔ robot bodies orchestrator |
| `robotics/brain/webui/nao_pipe.py` | **VPS (Open WebUI)** | Open WebUI Pipe — chat interface for NAO Brain |

> `app.py` is **not** meant to run on the robot — it is a VPS service. Deploying it on the NAO causes `ImportError: No module named flask` because Flask is not available on OpenNAO.

---

## `nao_ws_bridge.py` — embedded bridge (Python 2.7)

### Features

- **Outbound WebSocket** connection to rosbridge with automatic reconnect (exponential backoff)
- **Zero external dependencies** — pure Python 2.7 built-in WebSocket client (RFC 6455)
- **Module filtering** — enable only the capabilities you need via `--modules`
- **Wake word detection** — "ok nao" / "okay nao" via ALSpeechRecognition
- **VAD audio capture** — records speech after wake word, publishes WAV on `/nao/audio`
- **TTS method negotiation** — announces `mp3` or `text` to the VPS on connect
- **Motion bounds** — hard angle, speed, stiffness and walk bounds before ALMotion; motion decisions live in the NAO driver (see [Motion bounds](#motion-bounds-robot-side))
- **Post-flash provisioning** — applies autonomous life state, language, volume from JSON,
  plus the bridge's default modules and audio capture (`"bridge"` section)

### CLI options

```
python /home/nao/nao_ws_bridge.py --vps ws://DRONE_IP:9090/ [options]

--vps URL             rosbridge WebSocket URL (required unless --provision only)
--token TOKEN         Secret sent as "Authorization: Bearer TOKEN" (default: $ROSBRIDGE_TOKEN).
                      Not needed through the static drone relay (ADR 006), which adds it.
                      Must match ROSBRIDGE_TOKEN in Coolify; connections without it get 401.
--verbose             Also log every /nao/motion and /nao/status message
                      (off by default: one log line per 20 Hz motion command costs CPU)
--nao-ip IP           NAOqi IP address (default: 127.0.0.1)
--nao-port PORT       NAOqi port (default: 9559)
--modules MOD,...     Comma-separated modules to enable
                      (default: all except speech and audio_out)
                      Available: audio_out, gesture, led, motion, move, speech, stiffness, stop
--audio-capture       Enable wake word and VAD capture (off by default)
--no-audio-capture    Force it off, even if the provision file enables it
                      (both default to the "bridge" section of the --provision file;
                      the command-line flags win)
--tts-method METHOD   auto | mp3 | text (default: auto)
                        mp3  → VPS generates audio via edge-tts, sends on /nao/audio_out
                        text → VPS sends raw text, NAOqi TTS speaks it
                        auto → mp3 if NAOqi available, otherwise text
--provision [FILE]    Apply post-flash config before starting (default: nao_provision.json)
```

### Recommended configuration: emotive-only NAO (ADR 004)

Since [ADR 004](decisions/004-drone-statique.md) the static drone handles voice,
listening and the camera. The NAO only renders emotions with its motors and LEDs:

| Module / feature | Emotive-only NAO | Why |
|---|---|---|
| `led`, `motion`, `stiffness` | **on** | eyes and head driven by `cognitive_renderer`, head and arms by the LLM orders |
| `gesture`, `stop` | **on** | postures (`/nao/gesture`) and the safety stop |
| `speech`, `audio_out` | off | the static drone's speaker talks |
| audio capture (wake word ASR + VAD) | off (`--no-audio-capture`) | the static drone's microphones listen, `body-gateway` checks "ok nao" |
| `move` | off | the NAO stays in place; add it back if it must walk |

This is the default: the audio modules and audio capture are off unless
asked for, so the robot does not spend CPU on ALSpeechRecognition (wake word)
or TTS playback. Add `--modules led,motion,stiffness,gesture,stop` to also
turn `move` off:

```bash
python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ \
    --modules led,motion,stiffness,gesture,stop
```

or, with the same values in the `"bridge"` section of `nao_provision.json`
(as in `nao_provision.json.example`):

```bash
python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --provision
```

While the static drone (or the stream voice) speaks, `cognitive_renderer` makes the NAO move its eyes
and head so it looks like the one talking (see
[cognitive_renderer](#cognitive_renderer)).

**Back to the NAO's own voice** (no static drone): nothing to change on the VPS,
`body-gateway` already sends the speech to the NAO when no static drone is
connected. On the robot, turn the audio modules back on (they are off by
default), either with `--modules ...,speech,audio_out --audio-capture` or with:

```json
"bridge": {
  "modules": ["led", "motion", "stiffness", "gesture", "stop", "speech", "audio_out"],
  "audio_capture": true
}
```

then restart the bridge. The NAO audio code (TTS, MP3 playback, wake word, VAD)
is still in `nao_ws_bridge.py`.

### Constrained motor universe (motor profile)

NAOqi motor control crashes too easily, so the NAO lives in a small, fixed
motor universe. It **stays seated** (no posture change, no walk) and only moves
three chains: the head (`HeadYaw`, `HeadPitch`), the left arm and the right arm
(`ShoulderPitch`, `ShoulderRoll`, `ElbowYaw`, `ElbowRoll`). No wrist, no hand.

The **motor profile** `nao_motor_profile.json`, next to the script on the
robot, gives for each chain:

| Field | Meaning | Known by the LLM |
|---|---|---|
| `rest` | rest pose, one angle per joint | no |
| `start` | start pose, where every gesture leaves from | no |
| `range` | `[min, max]` per joint (rad), holds `start` | yes |
| `speed` | speed fraction of every move of the chain | no |

Every gesture is **rest → start → target → start → rest**. The LLM gives the
target as joint positions; a joint it does not give keeps its start value; a
target out of range is clamped. Because each range is a box holding the start
pose, the straight move from start to any target stays inside the range.
Sequences are run by the NAO driver ([Motion decisions](#motion-decisions-naoqi_driver)).

The profile is written by the **calibration**, run from the régie page
`/robot` ([below](#calibration-from-the-régie)). **Without a profile no
joint moves**: calibrate each chain once after installing the bridge. The
previous profile is kept as `nao_motor_profile.json.<version>.bak` at each save.

```json
{
  "version": 3, "calibrated_at": "2026-10-29T19:30:00", "posture": "Sit",
  "chains": {
    "head": {"rest":  {"HeadYaw": 0.0, "HeadPitch": 0.1},
             "start": {"HeadYaw": 0.0, "HeadPitch": -0.05},
             "range": {"HeadYaw": [-0.8, 0.8], "HeadPitch": [-0.3, 0.2]},
             "speed": 0.12},
    "larm": {...}, "rarm": {...}
  }
}
```

### Calibration from the régie

The régie page `/robot?token=REGIE_TOKEN` drives the calibration, chain by
chain. The bridge runs it on the robot (no VPS latency) and publishes its state
on `/nao/calibration_state` (live angles every 0.5 s). While a chain is being
calibrated, the bridge ignores every `/nao/motion` and `/nao/stiffness` order
for it and the eyes turn orange.

1. **Rest pose**: cut the stiffness of the chain, place it by hand, capture
   (average of 5 sensor reads).
2. **Start pose**: same.
3. **Ranges**: start the recording (stiffness off), sweep the allowed zone of
   each joint by hand, stop. The recorded min/max lose `range_margin` at each
   end, stay inside the hardware range and are widened to hold the start pose.
4. **Speed**: fraction of the motor max speed, capped by `max_speed`.
5. **Replay**: the robot plays rest → start → min then max of each joint →
   start → rest at that speed.
6. **Save** (only after a replay) writes the profile atomically: a crash
   during the calibration keeps the old one. **Cancel** keeps the old profile.
   Either way the chain goes back to its rest pose and the bridge announces
   the profile again (`/nao/capabilities`): the driver resets its sequences.

### NAOqi components (régie)

The same page switches the NAOqi components; their state is kept on the robot
in `nao_components.json` and applied when the bridge starts. Everything that
moves the robot on its own is **off by default**; smart stiffness and the
diagnosis effect protect the motors and stay on unless switched off.

| Component | Default | NAOqi call |
|---|---|---|
| `fall_manager` | off | `ALMotion.setFallManagerEnabled` (NAOqi refuses to turn it off until allowed in the robot's web page: the régie shows the error) |
| `arm_collision_protection` | off | `ALMotion.setCollisionProtectionEnabled("Arms")` |
| `external_collision` | off | `ALMotion.setExternalCollisionProtectionEnabled("All")` |
| `push_recovery` | off | `ALMotion.setPushRecoveryEnabled` |
| `breath` / `idle_posture` | off | `ALMotion.setBreathEnabled("Body")` / `setIdlePostureEnabled("Body")` |
| `basic_awareness` | off | `ALBasicAwareness.startAwareness` / `stopAwareness` |
| `expressive_listening` | off | `ALAutonomousMoves.setExpressiveListeningEnabled` |
| `smart_stiffness` | on | `ALMotion.setSmartStiffnessEnabled` |
| `diagnosis_effect` | on | `ALMotion.setDiagnosisEffectEnabled` |

### Motion bounds (robot side)

The robot is a puppet: motion **decisions** (sequences, postures, motor heat)
are taken by the NAO driver on the VPS, see
[Motion decisions](#motion-decisions-naoqi_driver). The bridge only keeps
hard **bounds**, a last safety net for an order that would bypass the driver:

- angles clamped to the NAO V5 hardware range shrunk by `joint_margin`,
  optionally narrowed per joint by `joint_ranges`, then to the motor profile
  box (range, rest and start pose of each chain); joints outside the profile,
  leg and unknown joints refused; no profile = every joint refused;
- joint speed clamped to `[min_speed, max_speed]`, posture speed `posture_speed`;
- `/nao/stiffness` values capped at `stiffness_max`;
- `moveToward` velocities capped at `walk_max`.

Joint moves are sent with the non-blocking `ALMotion.setAngles` and postures
run in their own thread, so the WebSocket receive thread never waits on a
motion. At startup (after a crash too) the bridge switches the NAOqi
components, turns the stiffness of each calibrated chain on without a jump
(current angles as target first) and moves it to its rest pose. It announces
the profile in `/nao/capabilities` and repeats it in every `/nao/status`.

Defaults, overridable in the `"bridge"` → `"motion_limits"` section of
`nao_provision.json` (used with `--provision`):

| Key | Default | Meaning |
|---|---|---|
| `max_speed` / `min_speed` | 0.3 / 0.02 | joint speed fraction range |
| `joint_margin` | 0.05 rad | distance kept from each mechanical stop |
| `joint_ranges` | `{}` | per-joint `[min, max]` in rad, can only narrow the range |
| `stiffness_max` | 1.0 | max stiffness value |
| `posture_speed` | 0.3 | `goToPosture` speed |
| `walk_max` | 0.5 | max normalized `moveToward` velocity |
| `fall_manager` / `arm_collision_protection` | false / false | startup state of these two NAOqi protections, until switched from the régie |
| `range_margin` | 0.02 rad | taken off each end of a range recorded in calibration |
| `motion_call` | `"setAngles"` | `"legacy"` restores the pre-limits call (`setStiffnesses` + blocking `angleInterpolationWithSpeed`) to compare when NAOqi crashes |

`--motor-profile FILE` changes the profile path (default
`nao_motor_profile.json` next to the script).

Tests without a robot (mock mode):

```bash
cd robotics/drones/nao/onboard && python3 -m unittest discover -s tests -v
```

### Usage examples

```bash
# All modules active (default):
python nao_ws_bridge.py --vps ws://DRONE_IP:9090/

# LEDs + speech only, no audio capture:
python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --modules led,speech --no-audio-capture

# Apply post-flash config then start bridge:
python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --provision

# Force native NAOqi TTS (no edge-tts on VPS):
python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --tts-method text
```

---

## ROS 2 topics

### VPS → NAO (commands)

| Topic | Type | Module | NAOqi action |
|---|---|---|---|
| `/nao/speech` | `std_msgs/String` | `speech` | `ALTextToSpeech.say()` — native TTS |
| `/nao/audio_out` | `std_msgs/String` (base64 MP3) | `audio_out` | `ALAudioPlayer.playFile()` — edge-tts audio |
| `/nao/cmd_vel` | `geometry_msgs/Twist` | `move` | `ALMotion.moveToward(x, y, theta)` |
| `/nao/gesture` | `std_msgs/String` | `gesture` | `ALRobotPosture.goToPosture()` |
| `/nao/leds` | `std_msgs/String` (JSON) | `led` | `ALLeds.fadeRGB(name, r, g, b, duration)` |
| `/nao/motion` | `std_msgs/String` (JSON) | `motion` | `ALMotion.angleInterpolationWithSpeed()` |
| `/nao/stop` | `std_msgs/Empty` | `stop` | `ALMotion.stopMove()` |
| `/nao/shutdown` | `std_msgs/Empty` | *(always active)* | Clean bridge shutdown |
| `/nao/calibration` | `std_msgs/String` (JSON) | *(always active)* | Calibration step from the régie, `{"op": "start", "chain": "rarm"}` … |
| `/nao/components` | `std_msgs/String` (JSON) | *(always active)* | NAOqi component on/off from the régie, `{"name": "breath", "enabled": false}` |

**LED payload format:**
```json
{"name": "AllLeds", "r": 0.0, "g": 0.0, "b": 1.0, "duration": 0.5}
```

**Motion payload format** (sent by the driver, one chain per message):
```json
{"joints": ["HeadYaw", "HeadPitch"], "angles": [0.15, -0.05], "speed": 0.12}
```

### NAO → VPS (sensors / telemetry)

| Topic | Type | Content | Interval |
|---|---|---|---|
| `/nao/status` | `std_msgs/String` (JSON) | `{"battery": 85, "motor_temp": {...}, "environment": {...}, "motor_profile": {...}}` — heartbeat | Every 30 s |
| `/nao/audio` | `std_msgs/String` (base64 WAV) | Captured speech after wake word | On demand |
| `/nao/capabilities` | `std_msgs/String` (JSON) | `{"tts_method": "mp3", "launch_pose": {...}, "motor_profile": {...}}` — TTS negotiation, angles read at launch, motor profile (`null` = not calibrated) | On connect, after a calibration |
| `/nao/calibration_state` | `std_msgs/String` (JSON) | calibration step, live angles, pending and saved profile | After each step, every 0.5 s during a calibration |
| `/nao/components_state` | `std_msgs/String` (JSON) | each NAOqi component: on/off, NAOqi error | On connect, after a switch |

---

## VPS-side pipeline (ROS 2)

```
Open WebUI (chat)                   NAO wake word "ok nao"
      │                                     │
      ▼                                     ▼
  nao_pipe.py                       nao_ws_bridge.py
      │                              captures audio (VAD)
      │                              publishes /nao/audio
      │                                     │
      └──────────► body_gateway ◄───────────┘
                  (port 8095)
                  calls Ollama
                  parses JSON actions
                       │
            ┌──────────┼──────────┐
            ▼          ▼          ▼
        /nao/speech  /nao/leds  /nao/motion  ...
            │
         nao-core/naoqi_driver
            │
         (via rosbridge)
            │
         nao_ws_bridge.py (on robot)
            │
         NAOqi SDK → speaker, LEDs, motors
```

### Motion decisions (naoqi_driver)

Every `/nao/motion`, `/nao/gesture` and `/nao/cmd_vel` order goes through
`motion_policy.py` in the NAO driver before reaching the robot:

- **Sequences**: from the motor profile announced by the robot, each chain
  (head, left arm, right arm) runs rest → start → target → start → rest. The
  target is the start pose with the ordered joints, clamped to their range;
  orders for the same chain within `merge_window` seconds form one target (the
  LLM often sends one action per joint). A new order during a gesture chains
  target → start → new target, without going back to rest. A chain idle for
  `rest_delay` seconds after its last step goes back to rest through the start
  pose. Chains move together, one `/nao/motion` message each.
- **Step duration**: `min_move_time`, or the estimated duration (largest
  distance ÷ (speed × joint max velocity × `velocity_scale`)); the next step
  leaves after it.
- **Refused**: everything without a profile, joints outside the three chains,
  and new orders for a chain whose hottest motor reaches `max_motor_temp` °C
  (from `/nao/status`; it can still go back to rest). Refused joints are
  counted in the log on every robot heartbeat.
- **Legs (postures and walk)**: off (`postures` empty, `walk` false): the NAO
  stays seated. A posture change running at the same time as
  `moveToward(0, 0, 0)` crashed NAOqi (2026-10-29). Once turned back on: only
  `postures` are allowed, `posture_cooldown` seconds apart; joint moves are
  blocked for `posture_block` seconds after a change, and during a walk; a
  posture is refused while walking, a walk during a posture change; a
  zero-velocity `move` is only sent to stop a walk in progress.

The driver publishes the ranges on `/bodies/{id}/sensors/motor_ranges`
(`{chain: {joint: [min, max]}}`, every 15 s): `body-gateway` writes them in the
LLM prompt. The LLM never sees the rest and start poses.

Joint max velocities (no-load motor speed 11 900 rpm ÷ gearbox ratio,
Aldebaran H25 motors doc): 8.29 rad/s for HeadYaw, ShoulderPitch, ElbowYaw;
7.19 rad/s for HeadPitch, ShoulderRoll, ElbowRoll; 24.62 rad/s for WristYaw.

Settings: the `MOTION_LIMITS` environment variable (JSON) overrides these defaults:

| Key | Default | Meaning |
|---|---|---|
| `min_interval` | 0.1 s | min delay between two sends of `/nao/motion` |
| `min_move_time` | 0.3 s | min duration of a sequence step |
| `velocity_scale` | 1.0 | share of the joint max velocity really reached (0 = only `min_move_time`) |
| `merge_window` | 0.3 s | orders of a chain within this delay form one target |
| `max_motor_temp` | 70 °C | new orders refused above it (0 = off) |
| `postures` | `[]` | allowed postures, e.g. `["Stand", "Sit"]` (empty = none) |
| `walk` | false | allow `/nao/cmd_vel` (walk) |
| `posture_cooldown` / `posture_block` | 5 s / 5 s | delay between two postures, joints blocked after one |
| `joints_while_busy` | false | accept joint moves during a posture change or walk |
| `rest_return` | true | go back to the rest pose when idle |
| `rest_delay` | 3 s | idle time after the last step before the return |

Tests without ROS 2: `cd robotics/drones/nao/driver && python3 -m unittest discover -s tests -v`.

### cognitive_renderer

The `cognitive_renderer_node.py` runs an animation loop (1 tick/minute) that maps cognitive state to LED colours and head movements (head orders go through the driver sequences like any other). Its arm poses are off by default: in the constrained motor universe the arms only follow the LLM. The energy level only sets the head stiffness: the arms keep the stiffness the robot gives them at rest (`RENDERER_ARMS=1` brings arm poses and arm stiffness back).

| State | LEDs | Head |
|---|---|---|
| `thinking` | blue pulsing | tilt down |
| `responding` | white steady | straight |
| `listening` | green steady | slight forward |
| `dreaming` | purple pulsing | slow micro-yaw |
| `idle` | dark blue drift | slow yaw oscillation |
| `uncertain` | yellow pulsing | slow yaw |
| `excited` | bright white | look up |

**Speaking sync (ADR 004, ADR 005).** `cognitive_renderer` listens to
`/bodies/{id}/sensors/speaking` for every voice body, `DRONE_ID` and
`OBS_DRONE_ID` (relayed by `static_driver` from `/drones/{id}/speaking`).
While any of them is `true`, every 1.5 s it blinks the
eyes (`FaceLeds`, bright/dim white) and nods the head slightly (`HeadPitch`),
so the NAO looks like it is the one speaking (with `RENDERER_ARMS=1` it also
raises one forearm then the other). When none is speaking, the NAO goes back to
its cognitive state profile.

| Variable (container `nao-core`) | Default | Role |
|---|---|---|
| `DRONE_ID` | `static_01` (`STATIC_DRONE_ID`) | static drone to mimic; empty = no sync |
| `OBS_DRONE_ID` | empty (compose: `obs_01`) | stream voice to mimic too ([ADR 005](decisions/005-avatar-twitch.md)) |
| `SPEAKING_SYNC` | `1` (`NAO_SPEAKING_SYNC`) | `0` disables the speaking animation |
| `SPEAKING_TIMEOUT` | `120` | stop after this many seconds without `speaking=false` (0 = never) |
| `RENDERER_ARMS` | `0` | `1` lets the profiles and the speaking animation move the arms and the energy set their stiffness |
| `MOTION_ENGINE` | `0` | `1` starts `motion_engine` (free angles at 20 Hz, off in the constrained universe) |

### Dream engine

`body_gateway` enters DREAMING state between 02:00-06:00 when battery ≥ 80%. Soft micro-actions (head, LEDs, whisper) are triggered every 5 minutes via the `_DREAM_SYSTEM` prompt.

---

## Deployment on the robot

### 1. Copy the script

```bash
# From the repository root on your dev machine:
scp robotics/drones/nao/onboard/nao_ws_bridge.py nao@NAO_IP:/home/nao/nao_ws_bridge.py
```

> **No additional Python packages needed.** The WebSocket client is built into the script (pure Python 2.7).

### 2. Launch the bridge manually

```bash
# Connect from NAO to the static drone relay (ADR 006), which adds the token
# and forwards to wss://rosbridge.$DOMAIN.
python /home/nao/nao_ws_bridge.py --vps ws://DRONE_IP:9090/
```

Successful connection logs:
```
[bridge] Connecting to ws://192.168.1.20:9090/ ...
[bridge] Connected. Active modules: audio_out, gesture, led, motion, move, speech, stop. Audio capture: yes.
[bridge] TTS capabilities announced: mp3
[bridge] Ready.
```

### 3. Automatic start on boot

Copy `nao_provision.json.example` to `/home/nao/nao_provision.json` (its
`"bridge"` section holds the [recommended emotive-only configuration](#recommended-configuration-emotive-only-nao-adr-004)),
then create `/home/nao/autostart.sh`:
```bash
#!/bin/bash
sleep 10   # wait for network
# Emotive-only NAO: modules and audio capture come from nao_provision.json ("bridge").
# No token here: the static drone relay adds it (ADR 006).
python /home/nao/nao_ws_bridge.py --vps ws://192.168.1.20:9090/ --provision &
```

Without a provisioning file, audio is already off; to turn `move` off too:
`--modules led,motion,stiffness,gesture,stop`.

> Replace `192.168.1.20` with the static drone's address on the local network.

Then in Choregraphe: **Edit** → **Preferences** → **Autorun** → add this script.

Alternatively edit `/home/nao/.config/autostart/` or the `autorun.ini` preference file depending on NAOqi firmware version.

---

## Verifying the connection

**On the VPS:**
```bash
# Check that rosbridge sees the robot connected
docker logs ros2-bridge -f
# Look for a line like: "Client connected from NAO_IP"

# Test TTS (robot must speak)
curl -X POST http://localhost:8095/say \
  -H "Content-Type: application/json" \
  -d '{"text": "Connection test", "robot_id": "nao_01"}'

# Run the full diagnostic suite
bash tests/diag-nao-connection.sh
```

**On the robot:**
```bash
ssh nao@NAO_IP
ps aux | grep nao_ws_bridge   # verify the process is running
```

---

## Troubleshooting

| Symptom | Likely cause | Solution |
|---|---|---|
| Bridge does not connect | Port 9090 blocked or wrong VPS IP | Check `ufw allow 9090` on VPS and the URL used |
| `ImportError: No module named websocket` | Old install attempt — not needed anymore | No wheel installation required; the WS client is built in |
| `Only ws:// is supported` | Trying to use `wss://` directly | Use `ws://` — TLS is terminated by Traefik upstream. Use stunnel if `wss://` is mandatory. |
| Robot does not speak | rosbridge not started or wrong topic | `docker logs ros2-bridge` on VPS |
| Wake word not detected | audio capture off (default, enable with `--audio-capture`) or ASR unavailable | Check ALSpeechRecognition is active on robot |
| Loop reconnections | Network timeout | Normal — script reconnects automatically |
| `[MOCK]` lines in logs | Running without NAOqi (on dev machine) | Expected in mock/dev mode — no action needed |

---

## SSL / WSS

Python 2.7 on the robot cannot do modern TLS, and the VPS only accepts rosbridge over `wss://` (443). Normally the NAO goes through the [static drone relay](#through-the-static-drone-adr-006): plain `ws://` on the local network, TLS from the drone to the VPS.

For a NAO without a drone, the robot must do TLS itself with `stunnel`:

```bash
# Install on NAO (if opkg available)
opkg install stunnel
```

Create `/home/nao/stunnel-rosbridge.conf`:
```ini
[rosbridge]
client  = yes
accept  = 127.0.0.1:9090
connect = rosbridge.your-domain.com:443
```

Then:
```bash
stunnel /home/nao/stunnel-rosbridge.conf &
python /home/nao/nao_ws_bridge.py --vps ws://127.0.0.1:9090 --token TOKEN
```

---

## Post-flash provisioning

Create `nao_provision.json` next to the script (see `nao_provision.json.example`):

```json
{
  "autonomous_life": {"state": "disabled"},
  "language": {
    "tts": "French",
    "asr": "French"
  },
  "system": {"volume": 70},
  "bridge": {
    "modules": ["led", "motion", "stiffness", "gesture", "stop"],
    "audio_capture": false
  }
}
```

The `"bridge"` section is optional: it gives the default `--modules` and audio
capture (emotive-only NAO here). Without it, the defaults are the same: every
module except `speech` and `audio_out`, and no audio capture.

The `"language"` section is applied only when the bridge uses it: the TTS
language with the `speech` module, the ASR language with audio capture. With
audio off, `setLanguage` would load the language engine into NAOqi's memory
for nothing, so it is skipped (`[provision] Audio off — TTS/ASR language skipped.`).

Apply on first boot:
```bash
python /home/nao/nao_ws_bridge.py --provision --vps ws://DRONE_IP:9090/
```

Or provision only (without starting the bridge):
```bash
python /home/nao/nao_ws_bridge.py --provision
```
