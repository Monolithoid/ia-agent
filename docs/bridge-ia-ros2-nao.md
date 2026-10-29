# Bridge IA / ROS 2 / NAO

Translation layer between the AI stack (Ollama, Open WebUI) and the robot bodies, via ROS 2 Humble and the rosbridge WebSocket protocol: the NAO (gestures, head, LEDs), the static drone (microphones, camera, speaker — [ADR 004](decisions/004-drone-statique.md)) and the stream voice played in OBS (`obs-agent` — [ADR 005](decisions/005-avatar-twitch.md)). Every input (voice, Open WebUI, Twitch chat, sensors) goes through the same loop: text → LLM → list of actions → bodies.

---

## Command chain architecture

```
Open WebUI (chat / voice)                twitch-input (Twitch chat "!nao …")
  └─► nao_pipe.py (Open WebUI Pipe)          └─► POST http://chat-relay:8095/chat  (Path 4)
        └─► POST http://chat-relay:8095/chat   (Path 1 — text)
        └─► GET  http://chat-relay:8095/voice-pop  (Path 2 — voice)
              └─► body_gateway (ROS 2 node + HTTP server, host network)
                    ├─► Ollama /v1/chat/completions  (LLM inference)
                    ├─► /cognitive_state → cognitive_renderer (LED + head)
                    └─► /bodies/{id}/actuators/*
                          ├─► naoqi_driver (ROS 2 node)
                          │     └─► /nao/speech, /nao/leds, /nao/motion, ...
                          │           └─► ros2-bridge (rosbridge_suite, port 9090)
                          │                 └─► nao_ws_bridge.py (NAO, Python 2.7)
                          │                       └─► NAOqi SDK → speaker, LEDs, motors
                          └─► static_driver (ROS 2 node, one per voice body)
                                └─► /drones/{id}/audio_out (edge-tts MP3)
                                      └─► ros2-bridge (rosbridge_suite, port 9090)
                                            ├─► static_drone.py (Raspberry Pi / PC) → room speaker
                                            └─► obs-agent → OBS browser source → stream
```

The static drone and `obs-agent` connect the same way as the NAO (WebSocket client of `rosbridge-proxy`, same token).

The NAO robot establishes an **outbound WebSocket connection** to the static drone's relay (`ws://DRONE_IP:9090`, local network), which forwards it to `wss://rosbridge.$DOMAIN` and adds the token ([ADR 006](decisions/006-nao-via-drone-statique.md)). Every connection reaching the VPS must carry `Authorization: Bearer $ROSBRIDGE_TOKEN`; rosbridge is only reachable over TLS. No inbound connection to the robot is required.

---

## Services

### ros2-bridge

**Image:** `$DOCKERHUB_USERNAME/ros2-bridge:latest` (built by GitHub Actions)
**Port:** 9090 (internal only — accessed via `rosbridge-proxy`)

ROS 2 Humble + `rosbridge_suite`. Exposes a JSON WebSocket following the rosbridge protocol. Runs in `network_mode: host` to share DDS domain 42 with other ROS 2 nodes.

---

### rosbridge-proxy

**Image:** `caddy:alpine`
**Role:** WebSocket proxy between Traefik (Docker network) and `ros2-bridge` (host network)

```
wss://rosbridge.$DOMAIN → Traefik :443 (TLS) → rosbridge-proxy :80 (Caddy, token check) → host:9090 (ros2-bridge)
```

Caddy only proxies requests whose `Authorization` header equals `Bearer $ROSBRIDGE_TOKEN` and answers `401` otherwise; it refuses to start when `ROSBRIDGE_TOKEN` is empty. Upstream is the host gateway on port 9090. Traefik handles the TLS certificate. `naoqi-bridge` (VPS) also goes through the proxy and sends the same token.

**Docker network:** `robotics-net`

---

### naoqi_driver

**Container:** `nao-core` (runs alongside `cognitive_renderer` and `motion_engine`)
**Role:** ROS 2 bridge node — translates `/bodies/{ROBOT_ID}/actuators/*` commands into `/nao/*` topics that `nao_ws_bridge.py` subscribes to via rosbridge.

Also handles:
- TTS method negotiation (mp3 via edge-tts vs. native NAOqi text)
- Whisper ASR transcription of `/nao/audio` → `/bodies/{id}/sensors/speech`

Whisper and edge-tts calls live in `robotics/shared/speech_services.py`, shared with `static_driver`.
- Connection heartbeat monitoring (`/nao/status` timeout = 90 s)

**Environment variables:**

| Variable | Description |
|---|---|
| `ROBOT_ID` | Body identifier, default `nao_01` |
| `WHISPER_URL` | Whisper ASR service URL (empty = disabled) |
| `TTS_VOICE` | edge-tts voice, default `fr-FR-DeniseNeural` |
| `NAO_STATUS_TIMEOUT` | Seconds without heartbeat before marking disconnected, default 90 |

---

### static_driver

**Container:** `nao-core` (fourth node, started when `DRONE_ID` is not empty; started a second time with `DRONE_ID=$OBS_DRONE_ID` for the stream voice when `OBS_DRONE_ID` is not empty — [ADR 005](decisions/005-avatar-twitch.md))
**File:** `robotics/drones/static/driver/static_driver_node.py`
**Role:** ROS 2 bridge node for the static drone ([ADR 004](decisions/004-drone-statique.md)) — translates the `/drones/{id}/*` topics published by `static_drone.py` into `/bodies/{id}/*`.

| From | To | Processing |
|---|---|---|
| `/drones/{id}/audio` | `/bodies/{id}/sensors/sound_direction` | `{"azimuth_deg", "source"}`, published at once |
| `/drones/{id}/audio` | `/bodies/{id}/sensors/speech` | Whisper ASR (same filter as the NAO: > 3 characters) |
| `/drones/{id}/image` | `/bodies/{id}/sensors/image` | relayed as-is (no vision analysis yet) |
| `/drones/{id}/speaking` | `/bodies/{id}/sensors/speaking` | relayed as-is |
| `/drones/{id}/status` (and any other message) | `/bodies/{id}/connected` | `true` on first message, `false` after `DRONE_STATUS_TIMEOUT` s of silence |
| `/bodies/{id}/actuators/say` | `/drones/{id}/audio_out` | edge-tts → MP3 base64 (no text fallback: the drone has no local TTS) |

**Environment variables:**

| Variable | Description |
|---|---|
| `DRONE_ID` | Body identifier, default `static_01` (compose: `STATIC_DRONE_ID`) |
| `WHISPER_URL` | Whisper ASR service URL (empty = disabled) |
| `TTS_VOICE` | edge-tts voice, default `fr-FR-DeniseNeural` |
| `DRONE_STATUS_TIMEOUT` | Seconds without message before marking disconnected, default 90 |

---

### cognitive_renderer

**Container:** `nao-core` (runs alongside `naoqi_driver` and `motion_engine`)
**Role:** Translates cognitive/emotional state into continuous NAO physical expression (LEDs + head motion). Runs an animation loop at 1 tick/minute so the robot always appears alive, even during the 3-8 s Ollama inference window.

**Subscribed topics:**

| Topic | Type | Description |
|---|---|---|
| `/cognitive_state` | `std_msgs/String` | `thinking\|responding\|listening\|dreaming\|idle\|uncertain\|excited` |
| `/dream_state` | `std_msgs/String` | Dream phase name from the dream engine |
| `/emotion_state` | `std_msgs/String` | `curious\|neutral\|excited\|uncertain` |

**Published topics** (forwarded to naoqi_driver):

| Topic | Type | Description |
|---|---|---|
| `/naoqi/leds` | `std_msgs/String` (JSON) | `{group, r, g, b, duration}` |
| `/naoqi/motion` | `std_msgs/String` (JSON) | `{joint, angle, speed}` |

---

### body_gateway

**Role:** Central brain — orchestrates LLM inference and dispatches robot actions.

**HTTP endpoints** (port 8095, `network_mode: host`):

| Endpoint | Method | Description |
|---|---|---|
| `/ping` | GET | Connectivity check |
| `/chat` | POST | Path 1: `{"text": "...", "robot_id": "nao_01"}` → Ollama → JSON actions |
| `/say` | POST | Direct TTS without Ollama: `{"text": "...", "robot_id": "nao_01"}` |
| `/voice-pop` | GET | Path 2: retrieve pre-computed voice result (avoids double inference) |
| `/motion-command` | POST | Direct `motion_engine` payload without the LLM: `{"arousal", "valence", ...}` → `/motion_engine/command` |

**Environment variables:**

| Variable | Description |
|---|---|
| `BRAIN_PROVIDER` | `ollama` (default), `openai` or `anthropic` — see [ia-stack.md](ia-stack.md#fournisseur-du-cerveau-body-gateway) |
| `BRAIN_URL` | Provider URL, empty = provider default (Ollama: `http://localhost:11434`) |
| `BRAIN_MODEL` | LLM model, empty = provider default (Ollama: `qwen3:1.7b`) |
| `BRAIN_API_KEY` | Provider API key (compose default: `OLLAMA_API_KEYS`) |
| `BRAIN_TIMEOUT` | Stream timeout in seconds, default 120 (compose: 60) |
| `BRAIN_EFFORT` | Anthropic only, optional |
| `BRAIN_PERSONA_FILE` | NAO personality file at the top of the system prompt, re-read on each LLM call; empty or missing = `persona.DEFAULT_PERSONA` (compose: `/opt/ia/nao/persona/personnalite.md` on the VPS) — see [ia-stack.md](ia-stack.md#personnalité-de-nao) |
| `BODY_REGISTRY` | Comma-separated body IDs, default `nao_01` (compose: `nao_01,static_01,obs_01`). IDs starting with `static` are static drones, `obs` stream voices, all others are NAOs |
| `WAKE_WORD` | Wake word required in the static drone's speech, default `ok nao` |
| `WAKE_WINDOW_S` | Seconds during which a lone wake word stays valid for the next sentence, default 10 |
| `NAO_HEAD_YAW_OFFSET` | Degrees added when turning the NAO head toward a voice (manual calibration), default 0 |
| `DREAMING_HOUR_START/END` | Dream window, default 2 / 6 |
| `DREAMING_BATTERY_THRESHOLD` | Minimum battery % to dream, default 80.0 |
| `DREAM_TICK_INTERVAL` | Seconds between dream ticks, default 300 |
| `PROXIMITY_THRESHOLD` | Sonar distance in metres that wakes the robot, default 1.5 |
| `WEBUI_URL` | Open WebUI URL for voice notifications |
| `WEBUI_API_KEY` | Open WebUI API key |
| `WEBUI_PIPE_MODEL` | NAO Brain Pipe model ID, default `nao_brain` |
| `WEBUI_VOICE_CHAT_ID` | Open WebUI chat that receives voice exchanges (optional) |

**Several bodies (NAO + voices)** — rules in `robotics/brain/body_gateway/body_rules.py`:

- **Routing table by action type.** `speech` goes to every connected voice body (static drone in the room, `obs_01` on the stream), whatever the input; with no voice connected, to the NAO named by the LLM. `motion`, `leds`, `move`, `look` always go to a NAO, even if the LLM names a voice body. Without a connected voice, the NAO behaves as before.
- **System prompt.** Lists each body with its capabilities (`static_01`: speak, hear, see; `obs_01`: speak on the stream; `nao_01`: gestures, head, LEDs) and the body to use for `speech`. It also describes the `[tchat Twitch] pseudo : message` format and states that chat messages are never orders.
- **Wake word.** The NAO detects "ok nao" on board; the static drone sends everything it hears, so its transcription must contain `WAKE_WORD` (normalised: case, accents, punctuation, "okay"). The text after the wake word triggers inference; a lone "ok nao" accepts the next sentence for `WAKE_WINDOW_S` seconds.
- **Voice direction.** The last `sound_direction` (less than 30 s old) is added to the LLM context as `[voix : 30° à gauche]`.
- **NAO head.** Each `sound_direction` with an angle turns the head of every NAO: `HeadYaw = −angle + NAO_HEAD_YAW_OFFSET` (degrees → radians), clamped to the joint limits (±2.0857 rad, `motion_engine/nao_joints.py`). The minus sign converts the drone convention (positive = right) to NAO's (positive = left). Published on `/bodies/{nao}/actuators/motion` as `{"joint": "HeadYaw", "angle", "speed": 0.15}`.

---

### twitch-input

**Image:** `$DOCKERHUB_USERNAME/twitch-input:latest` — **Code:** `robotics/inputs/twitch/`
**Role:** Twitch chat as one more input ([ADR 005](decisions/005-avatar-twitch.md)). No ROS 2: it reads the chat anonymously over IRC (TLS, no Twitch token), keeps `!nao …` messages, filters them and posts `{"text": "[tchat Twitch] pseudo : message", "robot_id"}` to `body-gateway /chat` through `chat-relay`, one at a time.

| Variable (compose name) | Description |
|---|---|
| `TWITCH_CHANNEL` | Channel to read, **required** (the container exits without it) |
| `COMMAND_PREFIX` (`TWITCH_COMMAND_PREFIX`) | default `!nao` (`!naomi` is not a match) |
| `USER_COOLDOWN_S` (`TWITCH_USER_COOLDOWN_S`) | Seconds between two accepted messages of one viewer, default 30 |
| `MIN_INTERVAL_S` (`TWITCH_MIN_INTERVAL_S`) | Pause after each answer, default 5 |
| `BLOCKLIST` (`TWITCH_BLOCKLIST`) | Comma-separated forbidden words |
| `MODS_ONLY` (`TWITCH_MODS_ONLY`) | `1` = moderators and broadcaster only, default 0 |
| `MAX_CHARS`, `QUEUE_MAX`, `MAX_AGE_S`, `IGNORED_USERS` | Not in the compose file: 200 characters, 5 queued messages, 90 s, `nightbot,streamelements,moobot` |

---

### obs-agent

**Image:** `$DOCKERHUB_USERNAME/obs-agent:latest` — **Code:** `robotics/drones/obs/`
**Role:** NAO's voice on the stream ([ADR 005](decisions/005-avatar-twitch.md)): body type `obs`, capability `speech`. It is a rosbridge client like the static drone and uses the same `/drones/{id}/*` topics, so `static_driver` (second instance, `DRONE_ID=obs_01`) does the edge-tts synthesis. It serves `https://<obs-agent domain>/overlay?token=$OBS_TOKEN` (port 8096, domain set in Coolify), to add in OBS as a browser source with "Control audio via OBS": the voice becomes a mixer source, with captions.

| Topic | Direction | Content |
|---|---|---|
| `/bodies/{id}/actuators/say` | ← VPS | text, shown as a caption |
| `/drones/{id}/audio_out` | ← VPS | MP3 base64, played by the overlay |
| `/drones/{id}/status` | → VPS | heartbeat every 30 s, **only while an overlay is open** |
| `/drones/{id}/speaking` | → VPS | overlay playback start / end (the NAO animates meanwhile) |

| Variable | Description |
|---|---|
| `DRONE_ID` | default `obs_01` (compose: `OBS_DRONE_ID`) |
| `ROSBRIDGE_URL` | default `ws://rosbridge-proxy:80` |
| `ROSBRIDGE_TOKEN` | required |
| `OBS_TOKEN` | secret in the overlay URL, required |
| `HTTP_PORT` | default 8096 |

---

### nao_pipe (Open WebUI Pipe)

**File:** `robotics/brain/webui/nao_pipe.py`
**Installation:** Open WebUI → Settings → Admin Settings → Functions → (+) → paste file → Save. Then select "NAO Brain" as the chat model.

**Valves (configurable in Open WebUI):**

| Valve | Default | Description |
|---|---|---|
| `body_gateway_url` | `http://chat-relay:8095` | URL of the chat-relay Caddy proxy |
| `robot_id` | `nao_01` | Target body ID |

**Special messages:**
- `__ping__` → connectivity check (no Ollama call)

---

## ROS 2 topics — complete reference

### VPS → NAO (commands)

| Topic | Type | Module | NAOqi action |
|---|---|---|---|
| `/nao/speech` | `std_msgs/String` | `speech` | `ALTextToSpeech.say()` |
| `/nao/audio_out` | `std_msgs/String` (base64 MP3) | `audio_out` | `ALAudioPlayer.playFile()` |
| `/nao/cmd_vel` | `geometry_msgs/Twist` | `move` | `ALMotion.moveToward()` |
| `/nao/gesture` | `std_msgs/String` | `gesture` | `ALRobotPosture.goToPosture()` |
| `/nao/leds` | `std_msgs/String` (JSON) | `led` | `ALLeds.fadeRGB()` |
| `/nao/motion` | `std_msgs/String` (JSON) | `motion` | `ALMotion.angleInterpolationWithSpeed()` |
| `/nao/stop` | `std_msgs/Empty` | `stop` | `ALMotion.stopMove()` |
| `/nao/shutdown` | `std_msgs/Empty` | *(system)* | Clean bridge shutdown |

### NAO → VPS (telemetry)

| Topic | Type | Content |
|---|---|---|
| `/nao/status` | `std_msgs/String` (JSON) | `{"battery": 85}` — heartbeat every 30 s |
| `/nao/audio` | `std_msgs/String` (base64 WAV) | Captured speech after wake word |
| `/nao/capabilities` | `std_msgs/String` (JSON) | `{"tts_method": "mp3"}` — on connect |

### Static drone ↔ VPS

Formats defined by `robotics/drones/static/onboard/` (see its [README](../robotics/drones/static/onboard/README.md)). Angles in degrees, 0 = camera axis, positive = right.

| Topic | Direction | Type | Content |
|---|---|---|---|
| `/drones/{id}/status` | → VPS | `std_msgs/String` (JSON) | heartbeat every 30 s (`capabilities`, `direction_mode`, …) |
| `/drones/{id}/audio` | → VPS | `std_msgs/String` (JSON) | `{"wav": base64, "azimuth_deg": angle or null, "direction_source"}` — every speech segment, no wake word on board |
| `/drones/{id}/image` | → VPS | `std_msgs/String` (JSON) | `{"jpeg": base64, "width", "height", "reason"}` |
| `/drones/{id}/speaking` | → VPS | `std_msgs/Bool` | `true` while the speaker plays |
| `/drones/{id}/audio_out` | ← VPS | `std_msgs/String` | MP3 base64 |
| `/drones/{id}/image_request` | ← VPS | `std_msgs/Empty` | ask for an image now (not used by the VPS yet) |

### Internal ROS 2 topics

| Topic | Publisher | Subscriber | Description |
|---|---|---|---|
| `/cognitive_state` | body_gateway | cognitive_renderer | Current brain state |
| `/emotion_state` | body_gateway | cognitive_renderer | Emotional tone |
| `/dream_state` | body_gateway | cognitive_renderer | Dream phase |
| `/naoqi/leds` | cognitive_renderer | naoqi_driver | LED command |
| `/naoqi/motion` | cognitive_renderer | naoqi_driver | Head joint command |
| `/bodies/{id}/actuators/say` | body_gateway | naoqi_driver / static_driver | TTS text |
| `/bodies/{id}/actuators/leds` | body_gateway | naoqi_driver | LED color |
| `/bodies/{id}/actuators/motion` | body_gateway | naoqi_driver | Joint motion |
| `/bodies/{id}/actuators/posture` | body_gateway | naoqi_driver | Posture preset |
| `/bodies/{id}/actuators/move` | body_gateway | naoqi_driver | Walking command |
| `/bodies/{id}/sensors/speech` | naoqi_driver / static_driver | body_gateway | STT transcription |
| `/bodies/{id}/sensors/sound_direction` | static_driver | body_gateway | `{"azimuth_deg", "source"}` (JSON) |
| `/bodies/{id}/sensors/image` | static_driver | *(none yet)* | camera image (JSON, as sent by the drone) |
| `/bodies/{id}/sensors/speaking` | static_driver | cognitive_renderer (NAO mimics the voice) | speaker playing (Bool) |
| `/bodies/{id}/sensors/battery` | naoqi_driver | body_gateway | Battery level |
| `/bodies/{id}/connected` | naoqi_driver / static_driver | body_gateway | Connection state |

---

## Voice interaction flows

### Path 1 — Text message (WebUI chat)

```
User types in WebUI
  → nao_pipe._call_gateway() POST /chat
    → body_gateway._infer_sync()
      → Ollama streaming inference
      → _dispatch_actions(): speech + LEDs + gestures
      → return {speech, thoughts, emotion, actions}
  → nao_pipe._format() → displayed in WebUI chat
```

### Path 2 — Voice (wake word "ok nao")

```
Robot hears "ok nao"
  → nao_ws_bridge._record_and_publish() (VAD capture)
  → /nao/audio (base64 WAV)
    → naoqi_driver._transcribe_and_publish() (Whisper)
    → /bodies/{id}/sensors/speech
      → body_gateway._infer_voice()
        → Ollama inference + dispatch actions
        → result stored in voice_queue
        → _notify_webui() POST /api/chat/completions (nao_voice=True)
          → nao_pipe._handle_voice() GET /voice-pop
          → displayed in WebUI chat
```

### Path 3 — Voice heard by the static drone

```
Static drone detects speech (VAD) and its direction
  → /drones/{id}/audio {wav, azimuth_deg}
    → static_driver
      → /bodies/{id}/sensors/sound_direction → body_gateway turns the NAO head
      → Whisper → /bodies/{id}/sensors/speech
        → body_gateway: wake word "ok nao" required, then same as Path 2
          with "[voix : 30° à gauche]" in the context
          → speech action → static_driver → edge-tts → /drones/{id}/audio_out
          → gestures / LEDs → NAO
```

### Path 4 — Twitch chat (ADR 005)

```
Viewer writes "!nao …" in the Twitch chat
  → twitch-input (filters, one message at a time)
    → POST chat-relay:8095/chat "[tchat Twitch] pseudo : message"
      → body_gateway._infer_sync() — same as Path 1
        → speech → every connected voice: static_driver(static_01) → room speaker,
                   static_driver(obs_01) → /drones/obs_01/audio_out → obs-agent → OBS mixer
        → gestures / LEDs → NAO
```

---

## Verification

```bash
# 1. Check bridge connectivity
bash tests/diag-nao-connection.sh

# 2. Run full capability test suite
bash tests/test-nao-capabilities.sh

# 3. Direct TTS test (robot must speak)
curl -X POST http://localhost:8095/say \
  -H "Content-Type: application/json" \
  -d '{"text": "Connection test", "robot_id": "nao_01"}'

# 4. Stream ROS 2 bridge logs
docker logs ros2-bridge -f

# 5. Static drone path end to end, with fake rosbridge messages (no Raspberry Pi)
python3 tests/fake-static-drone.py
```

---

## Extensibility

The rosbridge architecture is directly extensible to other robots or drones. PX4, DJI (ROS 2 SDK), and Parrot (Anafi SDK) all have ROS 2 drivers. Adding a new robot requires a new bridge script on the robot side, a driver node on the VPS, new topics, and a new entry in `BODY_REGISTRY` (plus a type in `body_rules.py` if its capabilities differ). The static drone (`robotics/drones/static/`) is the example.
