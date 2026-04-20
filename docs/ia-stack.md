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

## Bitterbot — agent IA autonome

**Image :** `$DOCKERHUB_USERNAME/bitterbot:latest` (buildée par GitHub Actions, publiée sur Docker Hub)  
**Port :** 19001 (lié à `127.0.0.1`)  
**Accès :** tunnel SSH uniquement

```bash
ssh -L 19001:localhost:19001 $VPS_USER@$DOMAIN
# WebSocket : ws://localhost:19001
```

Agent avec :
- **Mémoire long-terme** persistée en SQLite
- **Dream Engine** : consolidation de mémoire toutes les 2 heures
- **Inférence 100 % locale** via Ollama (pas d'API cloud)
- **API compatible OpenAI** sur le port 19001

**Isolation réseau :** `bitterbot-net` avec `internal: true` — aucun accès internet possible au niveau Docker. Seul Ollama est joignable via ce réseau.

| Variable | Description |
|---|---|
| `BITTERBOT_GATEWAY_TOKEN` | Secret d'auth WebSocket — doit correspondre à `gateway.auth.token` dans `bitterbot.json` |

**Volumes :**

| Chemin hôte | Chemin container | Mode |
|---|---|---|
| `/opt/ia/bitterbot/config` | `/home/node/.bitterbot` | rw |
| `/opt/ia/bitterbot/workspace` | `/home/node/.bitterbot/workspace` | rw |
| `/opt/ia/sync/onedrive` | `/home/node/.bitterbot/workspace/onedrive` | ro |
| `/opt/ia/sync/gdrive` | `/home/node/.bitterbot/workspace/gdrive` | ro |

**Modifier la configuration :** éditer `/opt/ia/bitterbot/config/bitterbot.json` puis redémarrer le container.

**Rebuild automatique :** GitHub Actions rebuild l'image tous les lundis à 3h00 UTC depuis le dépôt upstream Bitterbot.

---

## Réseaux Docker

```
ai-network (bridge, internet)
  └── ollama, open-webui, n8n, chromadb, nao-gateway

bitterbot-net (bridge, internal — aucun accès internet)
  └── bitterbot-gateway, ollama
```

Bitterbot et Ollama sont simultanément dans `bitterbot-net`. Ollama reste aussi dans `ai-network` pour servir Open WebUI et n8n.
