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

**Changer le modèle utilisé par body-gateway :** variable d'environnement `BRAIN_MODEL` dans Coolify.

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

Le mot de réveil **« ok nao »** active l'agent IA (Ollama / `BRAIN_MODEL`). Celui-ci répond en langage naturel ET pilote simultanément ses corps physiques (NAO, drones, tout corps dans `BODY_REGISTRY`) via ROS 2.

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
                                  Ollama (BRAIN_MODEL)
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
- Déclencher des actions sur le robot NAO via `body-gateway` (POST localhost:8095/chat)
- Chaîner des appels LLM via Ollama
- Automatiser des tâches sur calendrier ou événement

---

## ChromaDB — mémoire vectorielle

**Image :** `chromadb/chroma:latest`  
**Port :** 8000 (interne uniquement — jamais exposé)

Utilisé par Open WebUI pour le RAG (Retrieval-Augmented Generation). Stocke les embeddings des documents uploadés.

**Volume :** `/opt/ia/chroma` → index vectoriel persistant

---

## AUTOMATIC1111 — génération d'images Stable Diffusion

**Image :** `$DOCKERHUB_USERNAME/automatic1111:latest` (buildée via GitHub Actions)  
**Port :** 7860 (lié à `127.0.0.1` — non exposé publiquement)  
**Mode :** CPU uniquement (pas de GPU sur ce VPS)

AUTOMATIC1111 sert d'engine de génération d'images pour Open WebUI via son API REST (`--api`). Les images sont générées lentement (~5–15 min en 512×512 sur CPU) mais le résultat est identique à une installation GPU.

### Modèle par défaut : DreamShaper 8

Au premier démarrage, l'`entrypoint.sh` télécharge automatiquement **DreamShaper 8** (~2,1 Go) depuis HuggingFace (`Lykon/DreamShaper`) si le fichier n'est pas déjà présent dans le volume. Les démarrages suivants sont immédiats.

```
/opt/ia/automatic1111/models/Stable-diffusion/dreamshaper_8.safetensors
```

DreamShaper 8 est un checkpoint SD 1.5 non censuré, généraliste et de haute qualité — adapté aux personnages, décors et planches de BD.

### Ajouter d'autres modèles

Déposer directement le fichier `.safetensors` ou `.ckpt` dans le volume hôte :

```bash
# Exemple : télécharger un modèle depuis HuggingFace
wget -P /opt/ia/automatic1111/models/Stable-diffusion/ \
  https://huggingface.co/<auteur>/<repo>/resolve/main/<fichier>.safetensors

# Le modèle est immédiatement disponible sans redémarrage du container
curl http://localhost:7860/sdapi/v1/refresh-checkpoints -X POST
```

### Intégration Open WebUI

La connexion est automatique via les variables d'environnement du service `open-webui` :

```
ENABLE_IMAGE_GENERATION=True
IMAGE_GENERATION_ENGINE=automatic1111
AUTOMATIC1111_BASE_URL=http://automatic1111:7860
```

Dans Open WebUI, l'icône image apparaît dans la barre de saisie du chat. Cliquer dessus et entrer le prompt en anglais pour générer une image.

### Volumes

| Volume hôte | Contenu |
|---|---|
| `/opt/ia/automatic1111/models/` | Checkpoints SD, LoRA, VAE, embeddings, hypernetworks |
| `/opt/ia/automatic1111/outputs/` | Images générées (horodatées) |
| `/opt/ia/automatic1111/config/` | `config.json`, `styles.csv`, `ui-config.json` |

---

## MoonDream — agent de vision pour la création de BD

MoonDream est un modèle de vision-langage (VLM) léger (~1,8 B paramètres) distribué via Ollama. Il peut analyser des images et répondre à des questions sur leur contenu, ce qui en fait un assistant naturel pour relire et affiner des planches de BD générées avec A1111.

**Aucun nouveau container requis** — MoonDream tourne dans Ollama.

### Installation

```bash
docker exec -it ollama ollama pull moondream
```

Le modèle (~1,7 Go) est téléchargé une seule fois et persisté dans `/opt/ia/ollama/`.

### Utilisation dans Open WebUI

1. Ouvrir un chat dans Open WebUI
2. Sélectionner le modèle **moondream** dans la liste des modèles
3. Joindre une image (icône trombone) → la planche générée par A1111, une case, un personnage
4. Poser la question : cohérence, description, suggestions de dialogue, etc.

**Exemples de prompts :**
```
Décris cette planche de BD en détail.
Le personnage dans cette case est-il cohérent avec la description "chevalier médiéval en armure noire" ?
Que se passe-t-il dans cette scène ? Propose un dialogue pour les bulles.
Quels éléments graphiques manquent pour rendre cette case plus dynamique ?
```

### Workflow BD complet

```
1. Générer un panel   →  Open WebUI (icône image) → A1111 (DreamShaper 8)
                          prompt : "medieval knight, dark armor, comic book style, ink"

2. Analyser le panel  →  Open WebUI (modèle moondream) → joindre l'image générée
                          "Est-ce que l'armure est cohérente avec ma description ?"

3. Itérer             →  Affiner le prompt A1111 selon les retours de MoonDream
                          Ajouter des LoRA ou modifier le modèle SD si besoin
```

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
