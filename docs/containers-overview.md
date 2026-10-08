# Récapitulatif des containers

Vue synthétique de tous les containers du projet, leurs rôles, et leurs échanges d'informations.

---

## Tableau récapitulatif

| Dénomination | Description & Rôle | Containers en entrée → informations & format | Containers en sortie → informations & format |
|---|---|---|---|
| **ollama** | Moteur d'inférence LLM local. Sert les modèles de langage (ex. `qwen2.5:7b`) via une API REST compatible OpenAI. | `open-webui` → requêtes de complétion (HTTP/JSON, OpenAI chat format) ; `n8n` → requêtes d'inférence (HTTP/JSON) ; `body-gateway` → prompts système + messages (HTTP/JSON, OpenAI chat format) | `open-webui` ← tokens générés (HTTP/JSON, streaming SSE) ; `n8n` ← texte généré (HTTP/JSON) ; `body-gateway` ← réponse LLM (HTTP/JSON) |
| **open-webui** | Interface web de chat et de gestion des agents. Authentification Google OAuth. Accessible publiquement via Traefik sur `chat.$DOMAIN`. | `ollama` ← tokens générés (HTTP/JSON SSE) ; `chromadb` ← chunks vectoriels pertinents (HTTP/JSON) ; `chat-relay` ← réponses du body-gateway (HTTP/JSON) | `ollama` → requêtes de complétion (HTTP/JSON) ; `chromadb` → textes à indexer ou requêtes de similarité (HTTP/JSON) ; `chat-relay` → messages utilisateur (HTTP/JSON) |
| **n8n** | Orchestrateur de workflows d'automatisation. Accessible uniquement via tunnel SSH. | `ollama` ← texte généré (HTTP/JSON) | `ollama` → requêtes d'inférence (HTTP/JSON) |
| **chromadb** | Base de données vectorielle pour la mémoire RAG (Retrieval-Augmented Generation). Jamais exposée publiquement. | `open-webui` → textes à vectoriser ou requêtes de similarité (HTTP/JSON, protocole ChromaDB) | `open-webui` ← vecteurs / chunks correspondants (HTTP/JSON) |
| **whisper-asr** | Service de transcription vocale (Speech-to-Text) basé sur OpenAI Whisper. Langue par défaut : français. | `nao-core` → fichiers audio captés par le micro du NAO ou du drone statique (HTTP multipart/form-data, WAV) | `nao-core` ← texte transcrit (HTTP/JSON) |
| **naoqi-bridge** | Pont HTTP entre l'écosystème Docker et le SDK NAOqi (Python 2.7). Tourne en mode mock si le robot n'est pas connecté. | `nao-core` → commandes motrices / requêtes de statut (HTTP/JSON) | `nao-core` ← statut du robot, confirmations (HTTP/JSON) ; NAO physique ← commandes qi SDK (réseau local, protocole NAOqi binaire) |
| **nao-core** | Container ROS 2 (Humble) regroupant quatre nœuds : `naoqi_driver` (pont topics ↔ NAO, audio → Whisper, parole → edge-tts), `cognitive_renderer` (état cognitif → LEDs / tête), `motion_engine` (synthèse biomécanique émotion → mouvement) et `static_driver` (pont `/drones/{id}/*` ↔ `/bodies/{id}/*` du drone statique, mêmes Whisper et edge-tts via `robotics/shared/speech_services.py`). | `whisper-asr` ← texte transcrit (HTTP/JSON) ; `naoqi-bridge` ← statut robot (HTTP/JSON) ; ROS 2 DDS ← commandes publiées par body-gateway, topics `/nao/*` et `/drones/{id}/*` venus de rosbridge | `whisper-asr` → audio WAV (HTTP multipart) ; `naoqi-bridge` → commandes et requêtes (HTTP/JSON) ; ROS 2 DDS → données capteurs `/bodies/{id}/sensors/*` (parole, direction de la voix, image) ; ROS 2 DDS → `/nao/*` et `/drones/{id}/audio_out` (MP3) vers rosbridge |
| **body-gateway** | Nœud ROS 2 (Humble) faisant office de cerveau central. Route les inférences LLM vers les corps physiques (ROS 2) avec une table par type d'action (parole → drone statique s'il est connecté, gestes / tête / LEDs → NAO), tourne la tête du NAO vers la voix, gère les cycles de rêve (2h–6h) et expose un endpoint HTTP de chat. | ROS 2 DDS ← données capteurs des corps (`/bodies/{id}/sensors/*`, topics ROS 2) ; `ollama` ← réponses LLM (HTTP/JSON) | `ollama` → prompts système + historique (HTTP/JSON, OpenAI chat format) ; ROS 2 DDS → commandes motrices / parole (topics ROS 2) ; ROS 2 DDS → état cognitif (topic `/cognitive_state`) ; `open-webui` via `chat-relay` ← réponses chat (HTTP/JSON, port 8095) |
| **ros2-bridge** | Serveur rosbridge (ROS 2 Humble). Expose le bus ROS 2 (DDS domaine 42) en WebSocket pour clients externes. | NAO / drone statique / clients externes ← messages ROS 2 encapsulés (WebSocket, protocole rosbridge JSON) | ROS 2 DDS → messages traduits vers les nœuds internes (topics ROS 2) |
| **rosbridge-proxy** | Reverse-proxy Caddy. Fait le pont entre l'extérieur (TLS via Traefik) et le ros2-bridge en mode host. | Traefik → connexions WebSocket entrantes (WSS 443, WS 80 pour le NAO) ; exige `Authorization: Bearer $ROSBRIDGE_TOKEN` | `ros2-bridge` (host) ← connexions WebSocket (WS, port 9090) |
| **chat-relay** | Reverse-proxy Caddy léger. Rend le body-gateway (réseau host) accessible depuis l'ai-network pour open-webui. | `open-webui` → requêtes HTTP de chat (HTTP/JSON, port 8095) | `body-gateway` (host) ← requêtes HTTP relayées (HTTP/JSON, port 8095) |
| **bitterbot-gateway** *(désactivé)* | Agent autonome avec mémoire long-terme (SQLite) et moteur de rêves. Désactivé depuis l'ADR 003 au profit d'appels directs Ollama depuis body-gateway. | `ollama` ← texte généré (HTTP/JSON) | `ollama` → requêtes d'inférence (HTTP/JSON) |

---

## Flux de données par cas d'usage

### Conversation vocale avec le robot NAO

```
Micro NAO
  └─► nao-core/naoqi_driver (audio PCM)
        └─► whisper-asr (WAV → HTTP multipart)
              └─► nao-core/naoqi_driver (texte transcrit ← JSON)
                    └─► body-gateway (topic ROS 2 /bodies/{id}/sensors/speech)
                          └─► ollama (prompt → HTTP/JSON)
                                └─► body-gateway (réponse ← JSON)
                                      ├─► nao-core/naoqi_driver (commande parole → topic ROS 2)
                                      │     └─► NAO physique (TTS via naoqi-bridge)
                                      └─► nao-core/cognitive_renderer (état cognitif → topic /cognitive_state)
                                            └─► NAO physique (LEDs / animations)
```

### Conversation vocale via le drone statique (ADR 004)

```
Micros du drone statique (Raspberry Pi / PC)
  └─► rosbridge-proxy → ros2-bridge (/drones/{id}/audio : WAV + angle, JSON)
        └─► nao-core/static_driver
              ├─► body-gateway (/bodies/{id}/sensors/sound_direction)
              │     └─► nao-core/naoqi_driver (HeadYaw vers la voix) → NAO
              └─► whisper-asr (WAV → HTTP multipart)
                    └─► body-gateway (/bodies/{id}/sensors/speech, mot de réveil « ok nao »)
                          └─► ollama (prompt + « [voix : 30° à gauche] »)
                                └─► body-gateway
                                      ├─► nao-core/static_driver (parole → edge-tts)
                                      │     └─► /drones/{id}/audio_out (MP3) → haut-parleur du drone
                                      └─► nao-core/naoqi_driver (gestes, LEDs) → NAO
```

Sans drone statique connecté, la parole repart vers le NAO comme avant.

### Chat utilisateur via navigateur

```
Navigateur
  └─► open-webui (HTTPS / Traefik)
        ├─► ollama (inférence → HTTP/JSON SSE)
        ├─► chromadb (RAG → HTTP/JSON)
        └─► chat-relay → body-gateway (chat physique → HTTP/JSON :8095)
```

### Connexion NAO / drone statique → rosbridge (entrée de commandes externes)

```
NAO ou drone statique (WebSocket sortant)
  └─► Traefik (WS :80 pour le NAO, WSS :443)
        └─► rosbridge-proxy (Caddy :80, vérifie le jeton)
              └─► ros2-bridge (host :9090)
                    └─► ROS 2 DDS (domaine 42)
                          └─► nao-core / body-gateway
```

---

## Réseaux Docker

| Réseau | Type | Containers membres | Accès internet |
|---|---|---|---|
| `ai-network` | bridge | ollama, open-webui, n8n, chromadb, naoqi-bridge, whisper-asr, chat-relay | oui |
| `bitterbot-net` | bridge (interne) | *(réservé bitterbot-gateway)* | non |
| `robotics-net` | bridge | rosbridge-proxy | oui |
| host | — | ros2-bridge, nao-core, body-gateway | via hôte |
