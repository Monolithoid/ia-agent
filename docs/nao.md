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
                    └─► nao-core (naoqi_driver + cognitive_renderer + motion_engine)
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
- **Post-flash provisioning** — applies autonomous life state, language, volume from JSON,
  plus the bridge's default modules and audio capture (`"bridge"` section)

### CLI options

```
python /home/nao/nao_ws_bridge.py --vps ws://DRONE_IP:9090/ [options]

--vps URL             rosbridge WebSocket URL (required unless --provision only)
--token TOKEN         Secret sent as "Authorization: Bearer TOKEN" (default: $ROSBRIDGE_TOKEN).
                      Not needed through the static drone relay (ADR 006), which adds it.
                      Must match ROSBRIDGE_TOKEN in Coolify; connections without it get 401.
--nao-ip IP           NAOqi IP address (default: 127.0.0.1)
--nao-port PORT       NAOqi port (default: 9559)
--modules MOD,...     Comma-separated modules to enable (default: all)
                      Available: audio_out, gesture, led, motion, move, speech, stiffness, stop
--no-audio-capture    Disable wake word and VAD capture
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
| `led`, `motion`, `stiffness` | **on** | eyes, head and arms driven by `cognitive_renderer` and `motion_engine` |
| `gesture`, `stop` | **on** | postures (`/nao/gesture`) and the safety stop |
| `speech`, `audio_out` | off | the static drone's speaker talks |
| audio capture (wake word ASR + VAD) | off (`--no-audio-capture`) | the static drone's microphones listen, `body-gateway` checks "ok nao" |
| `move` | off | the NAO stays in place; add it back if it must walk |

```bash
python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ \
    --modules led,motion,stiffness,gesture,stop --no-audio-capture
```

or, with the same values in the `"bridge"` section of `nao_provision.json`
(as in `nao_provision.json.example`):

```bash
python nao_ws_bridge.py --vps ws://DRONE_IP:9090/ --provision
```

While the static drone (or the stream voice) speaks, `cognitive_renderer` makes the NAO move its eyes,
head and arms so it looks like the one talking (see
[cognitive_renderer](#cognitive_renderer)).

**Back to the NAO's own voice** (no static drone): nothing to change on the VPS,
`body-gateway` already sends the speech to the NAO when no static drone is
connected. On the robot, restore the audio modules, either by removing the
`"bridge"` section from `nao_provision.json` (all modules, audio capture on) or with:

```json
"bridge": {
  "modules": ["led", "motion", "stiffness", "gesture", "stop", "speech", "audio_out"],
  "audio_capture": true
}
```

then restart the bridge. The NAO audio code (TTS, MP3 playback, wake word, VAD)
is still in `nao_ws_bridge.py`.

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

**LED payload format:**
```json
{"name": "AllLeds", "r": 0.0, "g": 0.0, "b": 1.0, "duration": 0.5}
```

**Motion payload format:**
```json
{"joint": "HeadYaw", "angle": 0.15, "speed": 0.08}
```

### NAO → VPS (sensors / telemetry)

| Topic | Type | Content | Interval |
|---|---|---|---|
| `/nao/status` | `std_msgs/String` (JSON) | `{"battery": 85}` — battery level heartbeat | Every 30 s |
| `/nao/audio` | `std_msgs/String` (base64 WAV) | Captured speech after wake word | On demand |
| `/nao/capabilities` | `std_msgs/String` (JSON) | `{"tts_method": "mp3"}` — TTS negotiation | On connect |

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

### cognitive_renderer

The `cognitive_renderer_node.py` runs an animation loop (1 tick/minute) that maps cognitive state to LED colours and head movements:

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
eyes (`FaceLeds`, bright/dim white), nods the head slightly (`HeadPitch`) and
raises one forearm then the other, so the NAO looks like it is the one speaking.
When none is speaking, the NAO goes back to its cognitive state profile. Arm
gestures only play when arm stiffness is on (energy `medium` or above).

| Variable (container `nao-core`) | Default | Role |
|---|---|---|
| `DRONE_ID` | `static_01` (`STATIC_DRONE_ID`) | static drone to mimic; empty = no sync |
| `OBS_DRONE_ID` | empty (compose: `obs_01`) | stream voice to mimic too ([ADR 005](decisions/005-avatar-twitch.md)) |
| `SPEAKING_SYNC` | `1` (`NAO_SPEAKING_SYNC`) | `0` disables the speaking animation |
| `SPEAKING_TIMEOUT` | `120` | stop after this many seconds without `speaking=false` (0 = never) |

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

Without a provisioning file, pass the options directly:
`--modules led,motion,stiffness,gesture,stop --no-audio-capture`.

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
| Wake word not detected | `--no-audio-capture` set or ASR unavailable | Check ALSpeechRecognition is active on robot |
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
capture (emotive-only NAO here). Without it, all modules are on and the NAO
listens and speaks itself.

Apply on first boot:
```bash
python /home/nao/nao_ws_bridge.py --provision --vps ws://DRONE_IP:9090/
```

Or provision only (without starting the bridge):
```bash
python /home/nao/nao_ws_bridge.py --provision
```
