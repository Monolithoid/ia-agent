# ia-agent — Stack IA unifiée

Stack IA auto-hébergée sur VPS Ubuntu, déployée via Docker Compose et gérée par Coolify. Elle regroupe un moteur LLM, une interface de chat, un orchestrateur de workflows, une mémoire vectorielle, un agent autonome et une infrastructure robotique (NAO via ROS 2).

## Documentation

| Section | Description |
|---|---|
| [Coolify & VPS](docs/coolify-vps.md) | Prérequis VPS, déploiement Coolify, CI/CD, cloud sync |
| [Stack IA](docs/ia-stack.md) | Ollama, Open WebUI, n8n, ChromaDB, Bitterbot |
| [Bridge IA / ROS 2 / NAO](docs/bridge-ia-ros2-nao.md) | nao-gateway, ros2-bridge, topics ROS 2, API |
| [Robot NAO](docs/nao.md) | Bridge embarqué sur le robot, déploiement, autostart |

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
        RP_Chat["chat.monolithoid.com :443"]
        RP_Ollama["ollama.monolithoid.com :443"]
    end

    subgraph Docker["VPS monolithoid.com — Docker"]
        subgraph aiNet["ai-network"]
            WebUI["Open WebUI :8080"]
            Ollama["Ollama :11434"]
            N8N["n8n :5678"]
            Chroma["ChromaDB :8000"]
            Gateway["nao-gateway :8001"]
        end

        subgraph bbNet["bitterbot-net (internal)"]
            Bitterbot["Bitterbot :19001"]
        end

        subgraph robotNet["robotics-net"]
            ROS["ros2-bridge :9090"]
        end
    end

    User -->|HTTPS| RP_Chat --> WebUI
    User -->|HTTPS + API Key| RP_Ollama --> Ollama
    Dev -->|"ssh -L 5678"| N8N
    Dev -->|"ssh -L 19001"| Bitterbot
    Dev -->|"ssh -L 8001"| Gateway

    WebUI -->|OIDC| Google
    WebUI --> Ollama & Chroma
    N8N --> Ollama
    N8N -->|"HTTP + X-API-Key"| Gateway
    Bitterbot -->|bitterbot-net| Ollama

    Gateway -->|rosbridge WS| ROS
    NAO -->|"WS sortant :9090"| ROS
```

---

## Structure du dépôt

```
ia-agent/
├── core/bitterbot/          → Dockerfile + config template de l'agent Bitterbot
├── robotics/
│   └── nao_brain/
│       ├── naoqi_bridge/    → HTTP bridge Python 2.7 (NAOqi SDK)
│       ├── naoqi_driver/    → Nœud ROS 2 : topics → HTTP vers naoqi_bridge
│       ├── cognitive_renderer/ → Nœud ROS 2 : état cognitif → LEDs / tête
│       └── body_gateway/    → Routeur Bitterbot ↔ corps robotiques
├── docs/
│   ├── coolify-vps.md
│   ├── ia-stack.md
│   ├── bridge-ia-ros2-nao.md
│   ├── nao.md
│   ├── architecture.md
│   └── decisions/           → ADR (Architecture Decision Records)
├── .github/workflows/
│   ├── build-bitterbot.yml  → Build + push monolithoid/bitterbot
│   └── build-robotics.yml   → Build + push images robotiques
└── docker-compose.yml
```

---

## Accès rapide aux services

| Service | URL / commande | Auth |
|---|---|---|
| Open WebUI | [chat.monolithoid.com](https://chat.monolithoid.com) | Google SSO |
| API Ollama | `https://ollama.monolithoid.com` | `Authorization: Bearer $OLLAMA_API_KEYS` |
| n8n | `ssh -L 5678:localhost:5678 user@monolithoid.com` | Basic auth |
| Bitterbot | `ssh -L 19001:localhost:19001 user@monolithoid.com` | Token WS |
| nao-gateway | `ssh -L 8001:localhost:8001 user@monolithoid.com` | `X-API-Key` |
| rosbridge | Port 9090 public | — (NAO se connecte en sortant) |
