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
docker exec -it ollama ollama pull qwen3:1.7b   # modèle par défaut de body-gateway
```

**Changer le modèle utilisé par body-gateway :** variable d'environnement `BRAIN_MODEL` dans Coolify.

---

## Fournisseur du cerveau (body-gateway)

`body-gateway` peut faire appel à trois fournisseurs de LLM. Le choix se fait par
la variable `BRAIN_PROVIDER`, dans Coolify. Le code est dans
`robotics/brain/body_gateway/brain_client.py`. Quel que soit le fournisseur, le LLM
doit répondre en JSON strict, qui est ensuite lu par `_parse_json()`.

| `BRAIN_PROVIDER` | Appel | Défaut `BRAIN_URL` | Défaut `BRAIN_MODEL` |
|---|---|---|---|
| `ollama` (défaut) | `POST {BRAIN_URL}/v1/chat/completions` en streaming, avec `"options": {"think": false}` | `http://localhost:11434` | `qwen3:1.7b` |
| `openai` | le même endpoint, sans le champ `options` | `https://api.openai.com` | `gpt-4o-mini` |
| `anthropic` | SDK officiel `anthropic`, `messages.stream()` | URL du SDK (`https://api.anthropic.com`) | `claude-haiku-4-5` |

| Variable | Description |
|---|---|
| `BRAIN_PROVIDER` | `ollama`, `openai` ou `anthropic` |
| `BRAIN_URL` | Laisser vide pour prendre le défaut du fournisseur |
| `BRAIN_MODEL` | Laisser vide pour prendre le défaut du fournisseur |
| `BRAIN_API_KEY` | Clé du fournisseur. Sans valeur, c'est `OLLAMA_API_KEYS` qui est utilisée : pour `openai` ou `anthropic`, il faut donc la renseigner (`sk-...` de platform.openai.com, `sk-ant-...` de console.anthropic.com) |
| `BRAIN_TIMEOUT` | Délai maximal en secondes entre deux morceaux du flux (défaut 120, 60 dans le compose) |
| `BRAIN_EFFORT` | Anthropic uniquement, optionnel : `low`, `medium`, `high`… Rien n'est envoyé si la variable est vide |

Sans `BRAIN_PROVIDER`, rien ne change : Ollama local et `qwen3:1.7b`, comme avant.

Un abonnement ChatGPT ou Claude.ai ne donne pas accès à l'API. Il faut une clé API,
facturée à l'usage.

### Paramètres Anthropic

Ce qui est envoyé dépend du modèle. Les règles ci-dessous viennent de la documentation Anthropic d'octobre 2026.

- **temperature** : elle n'est envoyée qu'aux modèles qui l'acceptent (Haiku 4.5,
  Sonnet 4.5 / 4.6, Opus 4.x jusqu'à 4.6). Opus 4.7 et suivants, Sonnet 5 / 5.5 et
  Haiku 5.5 renvoient une erreur 400 avec une température non par défaut. Pour ces
  modèles, et pour tout modèle inconnu, la valeur par défaut de l'API s'applique.
- **thinking** : le paramètre n'est jamais envoyé. Haiku 4.5 répond alors sans
  réflexion, ce qui équivaut au `think: false` d'Ollama. Sur Opus 5.5 et Sonnet 5.5,
  la réflexion ne peut pas être désactivée (`disabled` renvoie une 400). Pour limiter
  la latence, mettre `BRAIN_EFFORT=low`.
- **effort** : Haiku 4.5 le refuse, d'où l'envoi uniquement si `BRAIN_EFFORT` est
  renseigné. Avec Opus 5.5 et Haiku 5.5, l'effort par défaut est `medium`.
- **structured outputs** (`output_config.format`) : non utilisés. Le format des
  `actions` varie selon leur type, et un schéma strict imposerait de figer chaque
  variante. La consigne JSON du prompt système et `_parse_json()` suffisent.
- Un refus du modèle (`stop_reason: "refusal"`) est traité comme une erreur : le
  robot passe sur sa réponse de secours.

Un modèle plus récent, `claude-haiku-5-5`, coûte 0,10 $ / 0,50 $ par million de
tokens, contre 1 $ / 5 $ pour Haiku 4.5. Sa réflexion est active par défaut : à
essayer avec `BRAIN_EFFORT=low`.

### Coût

La boucle des rêves (02 h – 06 h, un tick toutes les 300 s) fait au plus
**48 appels par nuit**. En comptant environ 1 000 tokens d'entrée (prompt système
et contexte capteurs) et 300 tokens de sortie par appel, l'ordre de grandeur est le
suivant :

| Modèle | Prix entrée / sortie (par million de tokens) | Par appel | Par nuit (48) | Par mois |
|---|---|---|---|---|
| `claude-haiku-4-5` | 1 $ / 5 $ | ≈ 0,0025 $ | ≈ 0,12 $ | ≈ 3,60 $ |
| `claude-haiku-5-5` | 0,10 $ / 0,50 $ | ≈ 0,00025 $ | ≈ 0,012 $ | ≈ 0,36 $ |
| `claude-opus-5-5` | 4 $ / 20 $, plus les tokens de réflexion | ≈ 0,01 $ ou plus | ≈ 0,50 $ ou plus | ≈ 15 $ ou plus |

Les conversations de jour (chat, voix, sonar, bumper) s'ajoutent à ce total, un
appel par interaction. Ces chiffres sont des estimations, pas des mesures : le coût
réel s'affiche dans la console du fournisseur.

### Confidentialité

Avec `openai` ou `anthropic`, **les données quittent le VPS**. Chaque appel envoie
au fournisseur le prompt système, le message de l'utilisateur (le texte du chat ou
la transcription de ce qui a été dit au robot) et le contexte des capteurs
(batterie, sonar, direction de la voix, températures moteur). Ces données suivent
alors la politique de conservation du fournisseur. Avec `ollama`, tout reste sur le VPS.

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

Le Pipe `NAO Brain` (`robotics/brain/webui/nao_pipe.py`) connecte le chat Open WebUI au **même pipeline complet** que la parole directe à NAO.

Chaque message du chat est envoyé au cerveau (`BRAIN_PROVIDER` / `BRAIN_MODEL`), sans mot de réveil : « ok nao » ne sert qu'à la voix. Le cerveau répond en langage naturel ET pilote simultanément ses corps physiques (NAO, voix de la pièce et du stream, tout corps dans `BODY_REGISTRY`) via ROS 2. Le tchat Twitch (`twitch-input`, [ADR 005](decisions/005-avatar-twitch.md)) arrive par le même endpoint `/chat`.

```
comment tu vas ?
dis bonjour et fais une révérence
mets les LEDs en violet et assieds-toi
```

**Flux :**

```
Chat WebUI  ──"[message]"──►  nao_pipe.py (Pipe)
                                            │
                              POST http://chat-relay:8095/chat
                                            │
                                     body-gateway (_infer_sync)
                                            │
                                  LLM (BRAIN_MODEL)
                                            │
                               JSON { thoughts, emotion, actions }
                                            │
                    ┌───────────────────────┘
                    │
              ROS 2 topics
              ├─ /bodies/{static_01,obs_01}/actuators/say → voix connectées
              │    (sinon /bodies/nao_01/actuators/say : le NAO parle)
              ├─ /bodies/nao_01/actuators/motion → geste
              └─ /bodies/nao_01/actuators/leds   → LEDs
                    │
              Réponse JSON retournée au chat
              (speech + actions affichées)
```

**Installation du Pipe (une seule fois, compte admin) :**

1. `https://chat.$DOMAIN` → **Workspace → Functions → (+)**
2. Coller le contenu de `robotics/brain/webui/nao_pipe.py`
3. Sélectionner le type **Pipe** → Enregistrer sous **NAO Brain**
4. Le modèle **NAO Brain** apparaît dans la liste des modèles du chat

Le Pipe communique avec `body-gateway` via `chat-relay:8095` (valve `body_gateway_url`) : `chat-relay` est un proxy Caddy sur `ai-network` qui relaie vers le port 8095 de l'hôte, où écoute `body-gateway` en `network_mode: host`.

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

## Bitterbot — agent IA autonome (désactivé)

> Voir [ADR 003](decisions/003-ollama-direct-sans-bitterbot.md) pour le contexte complet.

Bitterbot est commenté dans `docker-compose.yml`. `body-gateway` appelle
**le fournisseur choisi** par `BRAIN_PROVIDER` (par défaut Ollama local, `qwen3:1.7b` — voir [Fournisseur du cerveau](#fournisseur-du-cerveau-body-gateway)).

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
  └── ollama, open-webui, n8n, chromadb, whisper-asr, naoqi-bridge, chat-relay, twitch-input

bitterbot-net (bridge, internal — aucun accès internet)
  └── (réservé pour Bitterbot — voir ADR 003)

robotics-net (bridge, internet)
  └── rosbridge-proxy, naoqi-bridge, obs-agent
```
