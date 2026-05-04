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
│   └── bitterbot/        → Dockerfile + config de l'agent Bitterbot
├── robotics/
│   ├── ros2/             → Dockerfile ROS 2 Jazzy + rosbridge_suite
│   └── nao/
│       ├── nao_ws_bridge.py   → Script Python 2.7 embarqué sur NAO
│       └── gateway/           → FastAPI : n8n → rosbridge → NAO
├── docs/
│   ├── architecture.md   → ce fichier
│   └── decisions/        → ADR (Architecture Decision Records)
├── .github/workflows/    → CI/CD GitHub Actions
└── docker-compose.yml    → composition complète
```

## Documentation liée

- [Récapitulatif des containers](containers-overview.md) — tableau détaillé des échanges entre containers
- [Stack IA](ia-stack.md) — configuration de chaque service IA
- [Bridge IA / ROS 2 / NAO](bridge-ia-ros2-nao.md) — couche robotique
- [ADR 001](decisions/001-ros2-over-ros1.md) · [ADR 002](decisions/002-nao-websocket-bridge.md) · [ADR 003](decisions/003-ollama-direct-sans-bitterbot.md)

---

## Services Docker

| Container        | Rôle                          | Port       | Accès              |
|-----------------|-------------------------------|------------|--------------------|
| `ollama`         | Moteur LLM (Qwen3 8B)         | 11434      | Public (API key)   |
| `open-webui`     | Interface chat / agents       | 3000→8080  | Public (Google SSO)|
| `n8n`            | Orchestrateur de workflows    | 5678       | SSH tunnel         |
| `chromadb`       | Mémoire vectorielle           | 8000       | Interne uniquement |
| `bitterbot-gateway` | Agent IA avec mémoire      | 19001      | SSH tunnel         |
| `ros2-bridge`    | ROS 2 + rosbridge WebSocket   | 9090       | Public (NAO)       |
| `nao-gateway`    | FastAPI n8n → ROS 2           | 8001       | SSH tunnel         |

Les services robotiques (`ros2-bridge`, `nao-gateway`) nécessitent le profil Docker Compose `robotics` :

```bash
docker compose --profile robotics up -d
```

## Réseaux Docker

```
ai-network (bridge, internet)
  └── ollama, open-webui, n8n, chromadb, nao-gateway

bitterbot-net (bridge, internal — aucun accès internet)
  └── bitterbot-gateway, ollama

robotics-net (bridge, internet)
  └── ros2-bridge, nao-gateway
```

## Architecture robotique NAO

```
n8n
 │  HTTP POST /nao/say  {"text": "Bonjour"}
 │  Header: X-API-Key: <token>
 ▼
nao-gateway (FastAPI, VPS)
 │  WebSocket JSON (rosbridge protocol)
 ▼
ros2-bridge (ROS 2 Jazzy + rosbridge_suite, VPS)
 │  WebSocket JSON (rosbridge protocol)
 │  Topic: /nao/speech (std_msgs/String)
 ▼  ← connexion SORTANTE établie par NAO
NAO robot
 └── nao_ws_bridge.py
      └── NAOqi 2.x → ALTextToSpeech.say("Bonjour")
```

### Topics ROS 2

| Topic           | Type                    | Direction   | Action NAO                  |
|----------------|-------------------------|-------------|------------------------------|
| `/nao/cmd_vel`  | `geometry_msgs/Twist`   | VPS → NAO   | `ALMotion.moveToward()`      |
| `/nao/speech`   | `std_msgs/String`       | VPS → NAO   | `ALTextToSpeech.say()`       |
| `/nao/gesture`  | `std_msgs/String`       | VPS → NAO   | `ALRobotPosture.goToPosture()`|
| `/nao/leds`     | `std_msgs/String` (JSON)| VPS → NAO   | `ALLeds.fadeRGB()`           |
| `/nao/stop`     | `std_msgs/Empty`        | VPS → NAO   | `ALMotion.stopMove()`        |
| `/nao/status`   | `std_msgs/String` (JSON)| NAO → VPS   | Batterie + température CPU   |

### Déploiement du script sur NAO

```bash
# Copier le script sur NAO
scp robotics/nao/nao_ws_bridge.py nao@NAO_IP:/home/nao/

# SSH sur NAO
ssh nao@NAO_IP

# Installer websocket-client (Python 2.7)
pip install websocket-client==0.59.0

# Lancer le bridge (remplacer VPS_IP)
python /home/nao/nao_ws_bridge.py --vps ws://VPS_IP:9090

# Pour lancement automatique au démarrage NAO :
# Ajouter dans /home/nao/naoqi/preferences/autorun.ini ou via Choregraphe
```

## Extensibilité drones

L'architecture rosbridge est directement extensible aux drones.
PX4, DJI (ROS 2 SDK), et Parrot (Anafi SDK) ont tous des drivers ROS 2.
Il suffit d'ajouter un nouveau bridge script côté drone et de nouveaux topics côté gateway.
