# Bridge IA / ROS 2 / NAO

Translation layer between the AI stack (Ollama, Open WebUI) and the NAO robot, via ROS 2 Jazzy and the rosbridge WebSocket protocol.

---

## Command chain architecture

```
Open WebUI (chat / voice)
  └─► nao_pipe.py (Open WebUI Pipe)
        └─► POST http://chat-relay:8095/chat   (Path 1 — text)
        └─► GET  http://chat-relay:8095/voice-pop  (Path 2 — voice)
              └─► body_gateway (ROS 2 node + HTTP server, host network)
                    ├─► Ollama /v1/chat/completions  (LLM inference)
                    ├─► /cognitive_state → cognitive_renderer (LED + head)
                    └─► /bodies/{id}/actuators/*
                          └─► naoqi_driver (ROS 2 node)
                                └─► /nao/speech, /nao/leds, /nao/motion, ...
                                      └─► ros2-bridge (rosbridge_suite, port 9090)
                                            └─► nao_ws_bridge.py (NAO, Python 2.7)
                                                  └─► NAOqi SDK → speaker, LEDs, motors
```

The NAO robot establishes an **outbound WebSocket connection** to `ws://rosbridge.$ROSBRIDGE_DOMAIN` (port 9090 or proxied via Traefik on 443). No inbound connection to the robot is required.

---

## Services

### ros2-bridge

**Image:** `$DOCKERHUB_USERNAME/ros2-bridge:latest` (built by GitHub Actions)
**Port:** 9090 (internal only — accessed via `rosbridge-proxy`)

ROS 2 Jazzy + `rosbridge_suite`. Exposes a JSON WebSocket following the rosbridge protocol. Runs in `network_mode: host` to share DDS domain 42 with other ROS 2 nodes.

---

### rosbridge-proxy

**Image:** `caddy:alpine`
**Role:** WebSocket proxy between Traefik (Docker network) and `ros2-bridge` (host network)

```
wss://$ROSBRIDGE_DOMAIN → Traefik :443 (TLS) → rosbridge-proxy :80 (Caddy) → host:9090 (ros2-bridge)
```

Caddy proxies WebSocket connections to `host-gateway:9090`. Traefik handles the TLS certificate.

**Docker network:** `robotics-net`

---

### naoqi_driver

**Image:** built with the `robotics` profile
**Role:** ROS 2 bridge node — translates `/bodies/{ROBOT_ID}/actuators/*` commands into `/nao/*` topics that `nao_ws_bridge.py` subscribes to via rosbridge.

Also handles:
- TTS method negotiation (mp3 via edge-tts vs. native NAOqi text)
- Whisper ASR transcription of `/nao/audio` → `/bodies/{id}/sensors/speech`
- Connection heartbeat monitoring (`/nao/status` timeout = 90 s)

**Environment variables:**

| Variable | Description |
|---|---|
| `ROBOT_ID` | Body identifier, default `nao_01` |
| `WHISPER_URL` | Whisper ASR service URL (empty = disabled) |
| `TTS_VOICE` | edge-tts voice, default `fr-FR-DeniseNeural` |
| `NAO_STATUS_TIMEOUT` | Seconds without heartbeat before marking disconnected, default 90 |

---

### cognitive_renderer

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

**Environment variables:**

| Variable | Description |
|---|---|
| `BRAIN_URL` | Ollama URL, default `http://localhost:11434` |
| `BRAIN_MODEL` | LLM model, default `qwen2.5:7b` |
| `BRAIN_API_KEY` | Ollama API key |
| `BRAIN_TIMEOUT` | Ollama timeout in seconds, default 120 |
| `BODY_REGISTRY` | Comma-separated body IDs, default `nao_01` |
| `DREAMING_HOUR_START/END` | Dream window, default 2 / 6 |
| `DREAMING_BATTERY_THRESHOLD` | Minimum battery % to dream, default 80.0 |
| `WEBUI_URL` | Open WebUI URL for voice notifications |
| `WEBUI_API_KEY` | Open WebUI API key |

---

### nao_pipe (Open WebUI Pipe)

**File:** `robotics/nao_brain/webui/nao_pipe.py`
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

### Internal ROS 2 topics

| Topic | Publisher | Subscriber | Description |
|---|---|---|---|
| `/cognitive_state` | body_gateway | cognitive_renderer | Current brain state |
| `/emotion_state` | body_gateway | cognitive_renderer | Emotional tone |
| `/dream_state` | body_gateway | cognitive_renderer | Dream phase |
| `/naoqi/leds` | cognitive_renderer | naoqi_driver | LED command |
| `/naoqi/motion` | cognitive_renderer | naoqi_driver | Head joint command |
| `/bodies/{id}/actuators/say` | body_gateway | naoqi_driver | TTS text |
| `/bodies/{id}/actuators/leds` | body_gateway | naoqi_driver | LED color |
| `/bodies/{id}/actuators/motion` | body_gateway | naoqi_driver | Joint motion |
| `/bodies/{id}/actuators/posture` | body_gateway | naoqi_driver | Posture preset |
| `/bodies/{id}/actuators/move` | body_gateway | naoqi_driver | Walking command |
| `/bodies/{id}/sensors/speech` | naoqi_driver | body_gateway | STT transcription |
| `/bodies/{id}/sensors/battery` | naoqi_driver | body_gateway | Battery level |
| `/bodies/{id}/connected` | naoqi_driver | body_gateway | Connection state |

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

---

## Deploy the robotics profile

Services `ros2-bridge`, `naoqi_driver`, `cognitive_renderer`, and `body_gateway` require the `robotics` Docker Compose profile:

```bash
docker compose --profile robotics up -d
```

Coolify automatically applies the correct profile when configured in the resource options.

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
```

---

## Extensibility

The rosbridge architecture is directly extensible to other robots or drones. PX4, DJI (ROS 2 SDK), and Parrot (Anafi SDK) all have ROS 2 drivers. Adding a new robot requires a new bridge script on the robot side, new topics, and a new entry in `BODY_REGISTRY`.
