# Bridge IA / ROS 2 / NAO

Couche de traduction entre la stack IA (n8n, Bitterbot) et le robot NAO, via ROS 2 Jazzy et le protocole rosbridge WebSocket.

---

## Architecture de la chaîne de commande

```
n8n (workflow)
  └─► POST http://nao-gateway:8001/nao/say
      Header: X-API-Key: $GATEWAY_API_KEY
        └─► nao-gateway (FastAPI, VPS)
              └─► rosbridge WebSocket ws://ros2-bridge:9090
                    └─► ros2-bridge (ROS 2 Jazzy, VPS)
                          └─► Topic /nao/speech (std_msgs/String)
                                └─► nao_ws_bridge.py (NAO, Python 2.7)
                                      └─► ALTextToSpeech.say("Bonjour")
```

Le robot NAO établit une **connexion WebSocket sécurisée sortante** vers `wss://$ROSBRIDGE_DOMAIN` (port 443). Traefik termine le TLS et transmet à `rosbridge-proxy` (Caddy), qui proxifie vers `ros2-bridge` sur le réseau hôte. Aucune connexion entrante vers le robot n'est nécessaire.

---

## Services

### ros2-bridge

**Image :** `$DOCKERHUB_USERNAME/ros2-bridge:latest` (buildée par GitHub Actions)  
**Port :** 9090 (interne uniquement — accès via `rosbridge-proxy`)

ROS 2 Jazzy Jalisco + `rosbridge_suite`. Expose un WebSocket JSON suivant le protocole rosbridge. Tourne en `network_mode: host` pour partager le domaine DDS 42 avec les autres nœuds ROS 2.

**Réseau :** `network_mode: host`

---

### rosbridge-proxy

**Image :** `caddy:alpine`  
**Rôle :** proxy WebSocket entre Traefik (réseau Docker) et `ros2-bridge` (réseau hôte)

```
wss://$ROSBRIDGE_DOMAIN  →  Traefik :443 (TLS)  →  rosbridge-proxy :80 (Caddy)  →  host:9090 (ros2-bridge)
```

Caddy proxifie les connexions WebSocket vers `host-gateway:9090` (l'alias Docker vers le réseau hôte). Traefik gère le certificat TLS.

**Réseau Docker :** `robotics-net`

---

### nao-gateway

**Image :** `$DOCKERHUB_USERNAME/nao-gateway:latest` (buildée par GitHub Actions)  
**Port :** 8001 (lié à `127.0.0.1` — accès via SSH tunnel uniquement)  
**Documentation interactive :**

```bash
ssh -L 8001:localhost:8001 $VPS_USER@$DOMAIN
# ouvrir http://localhost:8001/docs
```

FastAPI qui expose des endpoints HTTP simples pour n8n et traduit les commandes en messages ROS 2 via rosbridge WebSocket.

| Variable | Description |
|---|---|
| `ROSBRIDGE_URL` | Adresse interne de ros2-bridge (`ws://ros2-bridge:9090`) |
| `GATEWAY_API_KEY` | Clé d'authentification pour le header `X-API-Key` |

**Réseaux Docker :** `ai-network` + `robotics-net` (passerelle entre les deux)

---

## Topics ROS 2

| Topic | Type | Direction | Action NAO |
|---|---|---|---|
| `/nao/cmd_vel` | `geometry_msgs/Twist` | VPS → NAO | `ALMotion.moveToward()` |
| `/nao/speech` | `std_msgs/String` | VPS → NAO | `ALTextToSpeech.say()` |
| `/nao/gesture` | `std_msgs/String` | VPS → NAO | `ALRobotPosture.goToPosture()` |
| `/nao/leds` | `std_msgs/String` (JSON) | VPS → NAO | `ALLeds.fadeRGB()` |
| `/nao/stop` | `std_msgs/Empty` | VPS → NAO | `ALMotion.stopMove()` |
| `/nao/status` | `std_msgs/String` (JSON) | NAO → VPS | Batterie + température CPU |

---

## Appels depuis n8n

Utiliser le nœud **HTTP Request** avec :
- **Method :** POST
- **URL :** `http://nao-gateway:8001/nao/<action>`
- **Header :** `X-API-Key: {{ $env.GATEWAY_API_KEY }}`
- **Body :** JSON selon l'action

Exemples de payloads :

```json
// POST /nao/say
{ "text": "Bonjour, je suis prêt." }

// POST /nao/move   (valeurs entre -1.0 et 1.0)
{ "linear_x": 0.3, "angular_z": 0.1 }

// POST /nao/gesture
{ "posture": "Stand" }

// POST /nao/leds
{ "name": "FaceLeds", "r": 0.0, "g": 1.0, "b": 0.0 }

// POST /nao/stop
{}
```

---

## Vérification

```bash
# 1. Le gateway répond
curl http://localhost:8001/health

# 2. Tester say (NAO doit être connecté au rosbridge)
curl -X POST http://localhost:8001/nao/say \
  -H "X-API-Key: $GATEWAY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Test de connexion"}'

# 3. Logs du bridge et du gateway
docker logs ros2-bridge -f
docker logs nao-gateway -f
```

---

## Extensibilité

L'architecture rosbridge est directement extensible à d'autres robots ou drones. PX4, DJI (ROS 2 SDK) et Parrot (Anafi SDK) ont tous des drivers ROS 2. Il suffit d'ajouter un nouveau bridge script côté robot et de nouveaux topics côté gateway.

---

## Déployer le profil robotique

Les services `ros2-bridge` et `nao-gateway` nécessitent le profil Docker Compose `robotics` :

```bash
docker compose --profile robotics up -d
```

Coolify déclenche automatiquement le bon profil si configuré dans les options de la resource.
