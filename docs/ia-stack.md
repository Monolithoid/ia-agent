# Stack IA

Services constituant le cœur de l'intelligence artificielle : moteur LLM, interface de chat, orchestrateur de workflows, mémoire vectorielle et agent autonome.

---

## Vue d'ensemble

```
Utilisateur (navigateur)
  └─► chat.$DOMAIN (Coolify/Traefik)
        └─► Open WebUI :8080
              ├─► Ollama :11434  (inférence LLM)
              └─► ChromaDB :8000 (RAG / mémoire vectorielle)

Développeur (SSH tunnel)
  └─► n8n :5678          (automatisations)
  └─► Bitterbot :19001   (agent autonome)
```

Tous ces services tournent dans `ai-network`, sauf Bitterbot qui est isolé dans `bitterbot-net` (accès internet bloqué au niveau Docker — seul Ollama est joignable).

---

## Ollama — moteur LLM

**Image :** `ollama/ollama:latest`  
**Port :** 11434 (exposé via Traefik avec clé d'API obligatoire)

| Variable | Description |
|---|---|
| `OLLAMA_API_KEYS` | Clé d'API (`openssl rand -hex 32`) |
| `OLLAMA_KEEP_ALIVE` | Maintien du modèle en mémoire (`24h`) |
| `OLLAMA_NUM_PARALLEL` | Requêtes simultanées (`2`) |
| `OLLAMA_NUM_THREAD` | Threads CPU alloués (`8`) |

**Volume :** `/opt/ia/ollama` → modèles IA (10–20 Go selon le modèle)

**Télécharger un modèle :**
```bash
docker exec -it ollama ollama pull qwen3:8b
```

**Changer le modèle utilisé par Bitterbot :** éditer `model:` dans `/opt/ia/bitterbot/config/bitterbot.json`.

---

## Open WebUI — interface de chat

**Image :** `ghcr.io/open-webui/open-webui:main`  
**Port :** 3000 → 8080 (exposé via Traefik)  
**URL :** `https://chat.$DOMAIN`

Accès restreint aux comptes Google autorisés via SSO (OIDC). Les inscriptions libres sont désactivées.

| Variable | Description |
|---|---|
| `WEBUI_SECRET_KEY` | Secret JWT pour les sessions |
| `WEBUI_ADMIN_EMAIL` | Email du compte administrateur |
| `OAUTH_CLIENT_ID` | Client ID Google (console.cloud.google.com) |
| `OAUTH_CLIENT_SECRET` | Secret Google |
| `OLLAMA_API_KEY` | Même valeur que `OLLAMA_API_KEYS` |

**Volume :** `/opt/ia/webui` → agents, utilisateurs, historique des conversations

**URI de callback OAuth à déclarer dans Google Cloud Console :**
```
https://chat.$DOMAIN/oauth/oidc/callback
```

### NAO Brain — chat WebUI comme interface du cerveau robotique

Le Pipe `NAO Brain` (`robotics/nao_brain/webui/nao_pipe.py`) connecte le chat Open WebUI au **même pipeline complet** que la parole directe à NAO.

Le mot de réveil **« ok nao »** active l'agent IA (Bitterbot / Qwen 2.5:7b). Celui-ci répond en langage naturel ET pilote simultanément ses corps physiques (NAO, drones, tout corps dans `BODY_REGISTRY`) via ROS 2.

```
ok nao, comment tu vas ?
ok nao, dis bonjour et fais une révérence
ok nao, avance vers la fenêtre
ok nao, mets les LEDs en violet et assieds-toi
```

**Flux :**

```
Chat WebUI  ──"ok nao, [message]"──►  nao_pipe.py (Pipe)
                                            │
                              POST http://host-gateway:8095/chat
                                            │
                                     body-gateway (_infer_sync)
                                            │
                               Bitterbot API (Qwen 2.5:7b)
                                            │
                               JSON { thoughts, emotion, actions }
                                            │
                    ┌───────────────────────┘
                    │
              ROS 2 topics
              ├─ /bodies/nao_01/actuators/say    → NAO parle
              ├─ /bodies/nao_01/actuators/motion → geste
              ├─ /bodies/nao_01/actuators/leds   → LEDs
              └─ /bodies/drone_01/actuators/...  → drone, etc.
                    │
              Réponse JSON retournée au chat
              (speech + actions affichées)
```

**Installation du Pipe (une seule fois, compte admin) :**

1. `https://chat.$DOMAIN` → **Workspace → Functions → (+)**
2. Coller le contenu de `robotics/nao_brain/webui/nao_pipe.py`
3. Sélectionner le type **Pipe** → Enregistrer sous **NAO Brain**
4. Le modèle **NAO Brain** apparaît dans la liste des modèles du chat

Le Pipe communique avec `body-gateway` via `host-gateway:8095` (port HTTP ouvert sur l'hôte Docker par le service `body-gateway` en `network_mode: host`).

---

## n8n — orchestrateur de workflows

**Image :** `n8nio/n8n:latest`  
**Port :** 5678 (lié à `127.0.0.1` — non exposé publiquement)  
**Accès :** tunnel SSH uniquement

```bash
ssh -L 5678:localhost:5678 $VPS_USER@$DOMAIN
# puis ouvrir http://localhost:5678
```

| Variable | Description |
|---|---|
| `N8N_BASIC_AUTH_PASSWORD` | Mot de passe de l'interface |

**Volume :** `/opt/ia/n8n` → définitions de workflows

**Usages typiques :**
- Déclencher des actions sur le robot NAO via le nao-gateway
- Chaîner des appels LLM via Ollama
- Automatiser des tâches sur calendrier ou événement

---

## ChromaDB — mémoire vectorielle

**Image :** `chromadb/chroma:latest`  
**Port :** 8000 (interne uniquement — jamais exposé)

Utilisé par Open WebUI pour le RAG (Retrieval-Augmented Generation). Stocke les embeddings des documents uploadés.

**Volume :** `/opt/ia/chroma` → index vectoriel persistant

---

## Bitterbot — agent IA autonome (désactivé)

> Voir [ADR 003](decisions/003-ollama-direct-sans-bitterbot.md) pour le contexte complet.

Bitterbot est commenté dans `docker-compose.yml`. `body-gateway` appelle
**Ollama directement** (`BRAIN_URL=http://localhost:11434`, `BRAIN_MODEL=qwen2.5:7b`).

Ce qu'on perd sans Bitterbot : mémoire long-terme SQLite, dream engine.  
Ce qu'on garde : tout le pipeline de contrôle physique et le chat WebUI.

**Pour réactiver Bitterbot :**

1. Décommenter `bitterbot-gateway` dans `docker-compose.yml`
2. Changer dans `body-gateway` : `BRAIN_URL=http://localhost:19001`
3. Redéployer

---

## Réseaux Docker

```
ai-network (bridge, internet)
  └── ollama, open-webui, n8n, chromadb, naoqi-bridge, chat-relay

bitterbot-net (bridge, internal — aucun accès internet)
  └── (réservé pour Bitterbot — voir ADR 003)

robotics-net (bridge, internet)
  └── rosbridge-proxy
```
