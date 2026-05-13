# ia-agent — Stack IA unifiée

Stack IA auto-hébergée sur VPS Ubuntu, déployée via Docker Compose et gérée par Coolify. Elle regroupe un moteur LLM, une interface de chat, un orchestrateur de workflows, une mémoire vectorielle, un agent autonome et une infrastructure robotique (NAO via ROS 2).

## Documentation

| Section | Description |
|---|---|
| [Architecture](docs/architecture.md) | Vision, infrastructure, réseaux Docker |
| [Récapitulatif des containers](docs/containers-overview.md) | Tableau synthétique de tous les containers, entrées/sorties, formats |
| [Stack IA](docs/ia-stack.md) | Ollama, Open WebUI, n8n, ChromaDB, AUTOMATIC1111, Bitterbot |
| [Bridge IA / ROS 2 / NAO](docs/bridge-ia-ros2-nao.md) | nao-core, body-gateway, ros2-bridge, topics ROS 2 |
| [Robot NAO](docs/nao.md) | Bridge embarqué sur le robot, déploiement, autostart |
| [Test de la chaîne](docs/test-chain.md) | Diagnostic maillon par maillon WebUI → NAO |
| [Coolify & VPS](docs/coolify-vps.md) | Prérequis VPS, déploiement Coolify, CI/CD, cloud sync |

### Décisions d'architecture (ADR)

| ADR | Décision |
|---|---|
| [ADR 001](docs/decisions/001-ros2-over-ros1.md) | ROS 2 plutôt que ROS 1 |
| [ADR 002](docs/decisions/002-nao-websocket-bridge.md) | Bridge WebSocket embarqué sur NAO (sans VPN ni Raspberry Pi) |
| [ADR 003](docs/decisions/003-ollama-direct-sans-bitterbot.md) | Ollama direct sans Bitterbot pour body-gateway |

---

## Architecture

```mermaid
flowchart TB
    subgraph Internet
        User(["Utilisateur\n(navigateur)"])
        Dev(["Développeur\n(SSH tunnel)"])
        Google["Google OAuth"]
        NAO["Robot NAO\n(réseau local)"]
    end

    subgraph Coolify["Reverse Proxy — Coolify / Traefik"]
        RP_Chat["chat.mon-domain.com :443"]
        RP_Ollama["ollama.mon-domain.com :443"]
        RP_ROS["rosbridge.mon-domain.com :443"]
    end

    subgraph Docker["VPS mon-domain.com — Docker"]
        subgraph aiNet["ai-network"]
            WebUI["Open WebUI :8080"]
            Ollama["Ollama :11434"]
            A1111["AUTOMATIC1111 :7860"]
            N8N["n8n :5678"]
            Chroma["ChromaDB :8000"]
            ChatRelay["chat-relay :8095"]
        end

        subgraph bbNet["bitterbot-net (internal)"]
            Bitterbot["Bitterbot :19001\n(désactivé)"]
        end

        subgraph hostNet["host network"]
            BodyGW["body-gateway :8095"]
            NaoCore["nao-core (ROS 2)"]
            Ros2Bridge["ros2-bridge :9090"]
        end

        subgraph robotNet["robotics-net"]
            RosbridgeProxy["rosbridge-proxy :80"]
        end
    end

    User -->|HTTPS| RP_Chat --> WebUI
    User -->|HTTPS + API Key| RP_Ollama --> Ollama
    Dev -->|"ssh -L 5678"| N8N
    Dev -.->|"ssh -L 19001"| Bitterbot

    WebUI -->|OIDC| Google
    WebUI --> Ollama & Chroma
    WebUI -->|génération image| A1111
    WebUI --> ChatRelay --> BodyGW
    N8N --> Ollama
    Bitterbot -.->|bitterbot-net| Ollama

    BodyGW --> Ollama
    BodyGW <-->|"ROS 2 DDS"| NaoCore
    NaoCore <-->|"ROS 2 DDS"| Ros2Bridge

    NAO -->|"WSS sortant :443"| RP_ROS --> RosbridgeProxy --> Ros2Bridge
```

---

## Structure du dépôt

```
ia-agent/
├── core/
│   ├── automatic1111/       → Dockerfile + entrypoint (Stable Diffusion CPU, DreamShaper 8)
│   └── bitterbot/           → Dockerfile + config template de l'agent Bitterbot
├── robotics/
│   ├── motion_engine/       → Package ROS 2 : synthèse biomécanique (émotion → mouvement)
│   ├── nao_core/            → Dockerfile + entrypoint du container nao-core
│   ├── shared/              → Classes partagées (RobotBridgeBase)
│   └── nao_brain/
│       ├── naoqi_bridge/    → HTTP bridge Python 2.7 (NAOqi SDK)
│       ├── naoqi_driver/    → Nœud ROS 2 : topics → HTTP vers naoqi_bridge
│       ├── cognitive_renderer/ → Nœud ROS 2 : état cognitif → LEDs / tête
│       ├── ros2_bridge/     → Dockerfile rosbridge_server WebSocket
│       ├── body_gateway/    → Cerveau ROS 2 : LLM ↔ corps robotiques
│       └── webui/           → Pipe Open WebUI (nao_pipe.py)
├── docs/
│   ├── coolify-vps.md
│   ├── ia-stack.md
│   ├── bridge-ia-ros2-nao.md
│   ├── nao.md
│   ├── architecture.md
│   └── decisions/           → ADR (Architecture Decision Records)
├── .github/workflows/
│   ├── build-bitterbot.yml  → Build + push $DOCKERHUB_USERNAME/bitterbot
│   └── build-robotics.yml   → Build + push images robotiques
└── docker-compose.yml
```

---

## Accès rapide aux services

| Service | URL / commande | Auth |
|---|---|---|
| Open WebUI | `https://chat.$DOMAIN` | Google SSO |
| API Ollama | `https://ollama.$DOMAIN` | `Authorization: Bearer $OLLAMA_API_KEYS` |
| n8n | `ssh -L 5678:localhost:5678 $VPS_USER@$DOMAIN` | Basic auth |
| AUTOMATIC1111 | `ssh -L 7860:localhost:7860 $VPS_USER@$DOMAIN` | — (accès local uniquement) |
| Bitterbot | `ssh -L 19001:localhost:19001 $VPS_USER@$DOMAIN` | Token WS |
| rosbridge | `wss://rosbridge.$DOMAIN` | — (NAO se connecte en sortant) |
