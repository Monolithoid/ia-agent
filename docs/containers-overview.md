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
| **whisper-asr** | Service de transcription vocale (Speech-to-Text) basé sur OpenAI Whisper. Langue par défaut : français. | `naoqi-driver` → fichiers audio captés par le micro du NAO (HTTP multipart/form-data, WAV) | `naoqi-driver` ← texte transcrit (HTTP/JSON) |
| **naoqi-bridge** | Pont HTTP entre l'écosystème Docker et le SDK NAOqi (Python 2.7). Tourne en mode mock si `NAO_IP` est absent. | `naoqi-driver` → commandes motrices / requêtes de statut (WebSocket, JSON) | `naoqi-driver` ← statut du robot, confirmations (WebSocket, JSON) ; NAO physique ← commandes qi SDK (réseau local, protocole NAOqi binaire) |
| **naoqi-driver** | Nœud ROS 2 (Humble) principal du robot NAO. Gère les capteurs, l'audio et le lien avec naoqi-bridge. | `whisper-asr` ← texte transcrit (HTTP/JSON) ; `naoqi-bridge` ← statut robot (WebSocket/JSON) ; ROS 2 DDS ← commandes publiées par body-gateway (topics ROS 2, messages std_msgs/String et custom) | `whisper-asr` → audio WAV (HTTP multipart) ; `naoqi-bridge` → commandes et requêtes (WebSocket/JSON) ; ROS 2 DDS → données capteurs `/bodies/{id}/sensors/*` (topics ROS 2) |
| **cognitive-renderer** | Nœud ROS 2 (Humble) d'expression émotionnelle. Traduit l'état cognitif du cerveau en animations LED et mouvements de tête du NAO. | ROS 2 DDS ← état cognitif publié par body-gateway (topic `/cognitive_state`, message JSON/String) | NAO physique ← commandes LEDs + animations (via naoqi-driver / DDS, topics ROS 2) |
| **body-gateway** | Nœud ROS 2 (Humble) faisant office de cerveau central. Route les inférences LLM vers les corps physiques (ROS 2), gère les cycles de rêve (2h–6h) et expose un endpoint HTTP de chat. | ROS 2 DDS ← données capteurs des corps (`/bodies/{id}/sensors/*`, topics ROS 2) ; `ollama` ← réponses LLM (HTTP/JSON) | `ollama` → prompts système + historique (HTTP/JSON, OpenAI chat format) ; ROS 2 DDS → commandes motrices / parole (topics ROS 2) ; ROS 2 DDS → état cognitif (topic `/cognitive_state`) ; `open-webui` via `chat-relay` ← réponses chat (HTTP/JSON, port 8095) |
| **ros2-bridge** | Serveur rosbridge (ROS 2 Humble). Expose le bus ROS 2 (DDS domaine 42) en WebSocket pour clients externes. | NAO / clients externes ← messages ROS 2 encapsulés (WebSocket, protocole rosbridge JSON) | ROS 2 DDS → messages traduits vers les nœuds internes (topics ROS 2) |
| **rosbridge-proxy** | Reverse-proxy Caddy. Fait le pont entre l'extérieur (TLS via Traefik) et le ros2-bridge en mode host. | Traefik → connexions WebSocket entrantes (HTTPS/WSS, port 443) | `ros2-bridge` (host) ← connexions WebSocket (WS, port 9090) |
| **chat-relay** | Reverse-proxy Caddy léger. Rend le body-gateway (réseau host) accessible depuis l'ai-network pour open-webui. | `open-webui` → requêtes HTTP de chat (HTTP/JSON, port 8095) | `body-gateway` (host) ← requêtes HTTP relayées (HTTP/JSON, port 8095) |
| **bitterbot-gateway** *(désactivé)* | Agent autonome avec mémoire long-terme (SQLite) et moteur de rêves. Désactivé depuis l'ADR 003 au profit d'appels directs Ollama depuis body-gateway. | `ollama` ← texte généré (HTTP/JSON) | `ollama` → requêtes d'inférence (HTTP/JSON) |

---

## Flux de données par cas d'usage

### Conversation vocale avec le robot NAO

```
Micro NAO
  └─► naoqi-driver (audio PCM)
        └─► whisper-asr (WAV → HTTP multipart)
              └─► naoqi-driver (texte transcrit ← JSON)
                    └─► body-gateway (topic ROS 2 /speech_input)
                          └─► ollama (prompt → HTTP/JSON)
                                └─► body-gateway (réponse ← JSON)
                                      ├─► naoqi-driver (commande parole → topic ROS 2)
                                      │     └─► NAO physique (TTS via naoqi-bridge)
                                      └─► cognitive-renderer (état cognitif → topic /cognitive_state)
                                            └─► NAO physique (LEDs / animations)
```

### Chat utilisateur via navigateur

```
Navigateur
  └─► open-webui (HTTPS / Traefik)
        ├─► ollama (inférence → HTTP/JSON SSE)
        ├─► chromadb (RAG → HTTP/JSON)
        └─► chat-relay → body-gateway (chat physique → HTTP/JSON :8095)
```

### Connexion NAO → rosbridge (entrée de commandes externes)

```
NAO (WebSocket sortant)
  └─► Traefik (WSS :443)
        └─► rosbridge-proxy (Caddy :80)
              └─► ros2-bridge (host :9090)
                    └─► ROS 2 DDS (domaine 42)
                          └─► naoqi-driver / body-gateway / cognitive-renderer
```

---

## Réseaux Docker

| Réseau | Type | Containers membres | Accès internet |
|---|---|---|---|
| `ai-network` | bridge | ollama, open-webui, n8n, chromadb, naoqi-bridge, whisper-asr, chat-relay | oui |
| `bitterbot-net` | bridge (interne) | *(réservé bitterbot-gateway)* | non |
| `robotics-net` | bridge | rosbridge-proxy | oui |
| host | — | ros2-bridge, naoqi-driver, cognitive-renderer, body-gateway | via hôte |
