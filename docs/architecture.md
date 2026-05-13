# Architecture — Stack IA personnelle unifiée

## Vision

Une seule IA avec des capacités modulaires.  
Le découpage en containers est une décision de scalabilité, pas de séparation fonctionnelle.

## Infrastructure

- **VPS Ubuntu** — ~24 GB RAM, sans GPU, géré via Coolify
- **Tous les services** tournent en containers Docker
- **Un seul `docker-compose.yml`** à la racine du monorepo

## Structure du monorepo

```
ia-agent/
├── core/
│   ├── automatic1111/       → Dockerfile + entrypoint (CPU, DreamShaper 8)
│   └── bitterbot/           → Dockerfile + config de l'agent Bitterbot
├── robotics/
│   ├── motion_engine/       → Package ROS 2 : synthèse biomécanique (émotion → mouvement)
│   ├── nao_core/            → Dockerfile + entrypoint du container nao-core
│   ├── shared/              → Classes partagées (RobotBridgeBase)
│   └── nao_brain/
│       ├── naoqi_bridge/    → HTTP bridge Python 2.7 (NAOqi SDK)
│       ├── naoqi_driver/    → Nœud ROS 2 : /bodies/*/actuators/* → /nao/*
│       ├── cognitive_renderer/ → Nœud ROS 2 : état cognitif → LEDs / tête
│       ├── ros2_bridge/     → Dockerfile rosbridge_server WebSocket
│       ├── body_gateway/    → Cerveau ROS 2 : LLM ↔ corps robotiques
│       └── webui/           → Pipe Open WebUI (nao_pipe.py)
├── docs/
│   ├── architecture.md      → ce fichier
│   └── decisions/           → ADR (Architecture Decision Records)
├── .github/workflows/       → CI/CD GitHub Actions
└── docker-compose.yml       → composition complète
```

## Documentation liée

- [Récapitulatif des containers](containers-overview.md) — tableau détaillé des échanges entre containers
- [Stack IA](ia-stack.md) — configuration de chaque service IA
- [Bridge IA / ROS 2 / NAO](bridge-ia-ros2-nao.md) — couche robotique
- [ADR 001](decisions/001-ros2-over-ros1.md) · [ADR 002](decisions/002-nao-websocket-bridge.md) · [ADR 003](decisions/003-ollama-direct-sans-bitterbot.md)

---

## Services Docker

| Container | Rôle | Port | Accès |
|---|---|---|---|
| `ollama` | Moteur LLM | 11434 | Public (API key) |
| `open-webui` | Interface chat / agents | 3000→8080 | Public (Google SSO) |
| `n8n` | Orchestrateur de workflows | 5678 | SSH tunnel |
| `chromadb` | Mémoire vectorielle | 8000 | Interne uniquement |
| `whisper-asr` | Transcription vocale (STT) | 9000 | Interne uniquement |
| `automatic1111` | Génération d'images Stable Diffusion (CPU) | 7860 | Interne uniquement |
| `naoqi-bridge` | HTTP bridge Python 2.7 → SDK NAOqi | 8090 | Interne (host) |
| `nao-core` | ROS 2 Humble : naoqi_driver + cognitive_renderer + motion_engine | — | host network |
| `body-gateway` | Cerveau ROS 2 : LLM ↔ corps robotiques | 8095 | host network |
| `ros2-bridge` | rosbridge_server WebSocket | 9090 | host network |
| `rosbridge-proxy` | Proxy Caddy : Traefik → ros2-bridge | 80 | robotics-net |
| `chat-relay` | Proxy Caddy : ai-network → body-gateway | 8095 | ai-network |
| `bitterbot-gateway` *(désactivé)* | Agent IA avec mémoire long-terme | 19001 | SSH tunnel |

## Réseaux Docker

```
ai-network (bridge, internet)
  └── ollama, open-webui, n8n, chromadb, whisper-asr, automatic1111, naoqi-bridge, chat-relay

bitterbot-net (bridge, internal — aucun accès internet)
  └── (réservé bitterbot-gateway — voir ADR 003)

robotics-net (bridge, internet)
  └── rosbridge-proxy

host network
  └── nao-core, body-gateway, ros2-bridge  (DDS ROS 2 domaine 42)
```

## Architecture robotique NAO

```
Open WebUI (chat)
  │  POST http://chat-relay:8095/chat
  ▼
chat-relay (Caddy, ai-network → host)
  │
  ▼
body-gateway (ROS 2 Humble, host network, :8095)
  │  POST http://localhost:11434/v1/chat/completions
  ├─► ollama (LLM)
  │
  │  ROS 2 DDS domain 42
  ├─► /cognitive_state → nao-core/cognitive_renderer (LEDs + tête)
  └─► /bodies/{id}/actuators/* → nao-core/naoqi_driver
                                        │  HTTP localhost:8090
                                        ▼
                                  naoqi-bridge (Python 2.7)
                                        │  qi SDK (mock si NAO absent)
                                        ▼
                                  NAO physique

NAO robot
  └── nao_ws_bridge.py (Python 2.7)
        └─► WebSocket sortant wss://rosbridge.$DOMAIN:443
              └─► rosbridge-proxy (Caddy) → ros2-bridge (host :9090)
                    └─► ROS 2 DDS domain 42
```

### Topics ROS 2

| Topic | Type | Direction | Action NAO |
|---|---|---|---|
| `/nao/speech` | `std_msgs/String` | VPS → NAO | `ALTextToSpeech.say()` |
| `/nao/audio_out` | `std_msgs/String` (base64 MP3) | VPS → NAO | `ALAudioPlayer.playFile()` |
| `/nao/cmd_vel` | `geometry_msgs/Twist` | VPS → NAO | `ALMotion.moveToward()` |
| `/nao/gesture` | `std_msgs/String` | VPS → NAO | `ALRobotPosture.goToPosture()` |
| `/nao/leds` | `std_msgs/String` (JSON) | VPS → NAO | `ALLeds.fadeRGB()` |
| `/nao/motion` | `std_msgs/String` (JSON) | VPS → NAO | `ALMotion.angleInterpolationWithSpeed()` |
| `/nao/stop` | `std_msgs/Empty` | VPS → NAO | `ALMotion.stopMove()` |
| `/nao/status` | `std_msgs/String` (JSON) | NAO → VPS | Batterie (heartbeat 30 s) |
| `/nao/audio` | `std_msgs/String` (base64 WAV) | NAO → VPS | Parole capturée (wake word) |

## Extensibilité drones

L'architecture rosbridge est directement extensible aux drones.
PX4, DJI (ROS 2 SDK) et Parrot (Anafi SDK) ont tous des drivers ROS 2.
Il suffit d'ajouter un script bridge côté drone et un nouvel ID dans `BODY_REGISTRY`.
