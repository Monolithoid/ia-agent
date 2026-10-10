# Coolify & VPS

Déploiement de la stack sur un VPS Ubuntu via Docker Compose géré par Coolify.

---

## 1. Prérequis sur le VPS

Se connecter en SSH, cloner ce dépôt, puis créer l'arborescence de données persistantes :

```bash
git clone https://github.com/monolithoid/ia-agent.git /opt/ia-agent
cd /opt/ia-agent
```

```bash
# Stack principale
mkdir -p /opt/ia/ollama /opt/ia/webui /opt/ia/n8n /opt/ia/chroma

# Robotique : SDK NAOqi (à extraire à la main) et modèles Whisper
mkdir -p /opt/ia/nao/naoqi-sdk /opt/ia/whisper
# Extraire pynaoqi-python2.7-2.1.4.*-linux64.tar.gz (portail SoftBank) dans /opt/ia/nao/naoqi-sdk/

# Bitterbot
mkdir -p /opt/ia/bitterbot/config /opt/ia/bitterbot/workspace
mkdir -p /opt/ia/sync/onedrive /opt/ia/sync/gdrive
chown -R 1000:1000 /opt/ia/bitterbot   # uid node dans le container

# Copier le template de config Bitterbot
cp core/bitterbot/bitterbot.json /opt/ia/bitterbot/config/bitterbot.json
# Éditer ce fichier : renseigner BITTERBOT_GATEWAY_TOKEN (même valeur que dans Coolify)
```

Un script `setup-vps.sh` à la racine du dépôt automatise ces étapes (à lancer depuis le clone) :

```bash
sudo bash /opt/ia-agent/setup-vps.sh
```

---

## 2. Firewall (UFW)

```bash
ufw allow 22
ufw allow 80
ufw allow 443
ufw allow from 172.16.0.0/12 to any port 8095  # chat-relay (Docker bridge) → body-gateway
ufw allow from 10.0.0.0/8 to any port 8095     # idem, autre plage Docker
ufw allow from 172.16.0.0/12 to any port 9090  # rosbridge-proxy (Docker bridge) → ros2-bridge
ufw allow from 10.0.0.0/8 to any port 9090     # idem, autre plage Docker
ufw deny 9090   # rosbridge n'est joignable que via Traefik + rosbridge-proxy (jeton)
ufw deny 11434
ufw deny 3000
ufw deny 5678
ufw deny 8095   # body-gateway n'est joignable que depuis les réseaux Docker (chat-relay)
ufw enable
```

`setup-vps.sh` applique ces règles (et ouvre aussi le port 8000 de l'interface Coolify).

> Le port 19001 (Bitterbot) n'est pas exposé directement. Le port 9090 (rosbridge) n'est plus exposé : tout passe par `rosbridge-proxy`, qui exige l'en-tête `Authorization: Bearer $ROSBRIDGE_TOKEN`. Tous les clients utilisent `wss://rosbridge.$DOMAIN` (443) ; le NAO (Python 2.7, sans TLS) passe par le relais du drone statique ([ADR 006](decisions/006-nao-via-drone-statique.md)), il n'y a plus de rosbridge en HTTP clair. Si une ancienne règle `ufw allow from NAO_PUBLIC_IP to any port 9090` existe, la supprimer avec `ufw delete`.

---

## 3. DNS

Faire pointer vers l'IP du VPS (enregistrements A) les sous-domaines servis par Traefik :

| Sous-domaine | Service |
|---|---|
| `chat.$DOMAIN` | Open WebUI |
| `ollama.$DOMAIN` | API Ollama |
| `rosbridge.$DOMAIN` | `rosbridge-proxy` (NAO, drone statique) |
| `obs.$DOMAIN` | `obs-agent` : overlay OBS, voix du stream ([ADR 005](decisions/005-avatar-twitch.md)) |

---

## 4. Configurer le projet dans Coolify

1. Coolify → **Projects** → créer le projet **"IA"**
2. **New Resource** → **Docker Compose** → pointer sur ce dépôt Git
3. Laisser le chemin du compose sur `docker-compose.yml` (racine)
4. Aller dans **Environment Variables** et ajouter toutes les variables ci-dessous

---

## 5. Variables d'environnement

Toutes les variables sensibles sont injectées par Coolify. Aucun secret n'est commité dans le dépôt.

| Variable | Comment la générer / obtenir |
|---|---|
| `DOMAIN` | Ton domaine racine (ex : `mon-domain.com`) |
| `DOCKERHUB_USERNAME` | Ton username Docker Hub |
| `OLLAMA_API_KEYS` | `openssl rand -hex 32` |
| `WEBUI_SECRET_KEY` | `openssl rand -hex 32` |
| `WEBUI_ADMIN_EMAIL` | Ton adresse email |
| `OAUTH_CLIENT_ID` | [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Credentials → OAuth 2.0 Client ID |
| `OAUTH_CLIENT_SECRET` | Idem, même credential |
| `N8N_BASIC_AUTH_PASSWORD` | Mot de passe fort de ton choix |
| `BITTERBOT_GATEWAY_TOKEN` | `openssl rand -hex 32` — doit correspondre à `gateway.auth.token` dans `bitterbot.json` |
| `ROSBRIDGE_TOKEN` | `openssl rand -hex 32` — jeton exigé par `rosbridge-proxy` ; même valeur à passer au NAO (`nao_ws_bridge.py --token`), au drone statique et à `obs-agent`. Sans lui, `rosbridge-proxy` refuse de démarrer |
| `TWITCH_CHANNEL` | Chaîne dont on lit le tchat (ex : `monolithoid`). Sans elle, `twitch-input` refuse de démarrer et redémarre en boucle |
| `OBS_TOKEN` | `openssl rand -hex 32` — secret de l'URL de l'overlay OBS. Sans lui (ou sans `ROSBRIDGE_TOKEN`), `obs-agent` refuse de démarrer |

Variables optionnelles (valeurs par défaut dans `docker-compose.yml`, dont l'en-tête décrit chacune) :

| Groupe | Variables |
|---|---|
| Cerveau ([Stack IA](ia-stack.md#fournisseur-du-cerveau-body-gateway)) | `BRAIN_PROVIDER`, `BRAIN_URL`, `BRAIN_MODEL`, `BRAIN_API_KEY`, `BRAIN_EFFORT`, `BRAIN_TIMEOUT` |
| Corps | `NAO_ROBOT_ID`, `STATIC_DRONE_ID`, `OBS_DRONE_ID` (vide = corps désactivé), `NAO_HEAD_YAW_OFFSET`, `NAO_SPEAKING_SYNC` |
| Voix | `WAKE_WORD`, `WAKE_WINDOW_S`, `LISTEN_DURATION`, `ASR_MODEL`, `TTS_VOICE` |
| Twitch | `TWITCH_COMMAND_PREFIX`, `TWITCH_USER_COOLDOWN_S`, `TWITCH_MIN_INTERVAL_S`, `TWITCH_BLOCKLIST`, `TWITCH_MODS_ONLY` |
| Rêves | `DREAMING_HOUR_START`, `DREAMING_HOUR_END`, `DREAMING_BATTERY_THRESHOLD`, `DREAM_TICK_INTERVAL`, `TZ` |
| Notifications vocales Open WebUI | `WEBUI_URL`, `WEBUI_API_KEY`, `WEBUI_PIPE_MODEL`, `WEBUI_VOICE_CHAT_ID` |

**URI de callback à déclarer dans Google Cloud Console :**
```
https://chat.$DOMAIN/oauth/oidc/callback
```

---

## 6. Pipeline CI/CD des images Docker

Sept images sont buildées automatiquement par GitHub Actions et publiées sur Docker Hub. Le VPS fait uniquement `docker pull` — aucun build sur le serveur.

### a) Créer un Access Token Docker Hub

Docker Hub → Account Settings → **Security** → **New Access Token**
- Description : `github-actions-ia-agent`
- Permissions : `Read & Write`
- Copier le token (affiché une seule fois)

### b) Ajouter les secrets dans GitHub

GitHub → repo → **Settings** → **Secrets and variables** → **Actions** :

| Nom | Valeur |
|---|---|
| `DOCKERHUB_USERNAME` | Ton username Docker Hub |
| `DOCKERHUB_TOKEN` | Le token Docker Hub |

### c) Déclencher les premiers builds

GitHub → **Actions** :
- Workflow **"Build & Push — Bitterbot image"** → **Run workflow**
- Workflow **"Build & Push — Images robotiques"** → **Run workflow**

Chaque build prend 5–15 min. Les images produites :
- `$DOCKERHUB_USERNAME/bitterbot:latest`
- `$DOCKERHUB_USERNAME/naoqi-bridge:latest`
- `$DOCKERHUB_USERNAME/nao-core:latest`
- `$DOCKERHUB_USERNAME/body-gateway:latest`
- `$DOCKERHUB_USERNAME/ros2-bridge:latest`
- `$DOCKERHUB_USERNAME/twitch-input:latest`
- `$DOCKERHUB_USERNAME/obs-agent:latest`

**Rebuilds automatiques :**
- Bitterbot : tous les lundis à 3h00 UTC
- Images robotiques : à chaque push sur `main` qui modifie `robotics/**`

---

## 7. Démarrer la stack

Dans Coolify → resource **"IA"** → bouton **Deploy** (ou **Start**).

Coolify exécute `docker compose pull && docker compose up -d`. Les données dans `/opt/ia/` sont préservées entre les redémarrages.

**Premier démarrage — télécharger un modèle LLM :**
```bash
docker exec -it ollama ollama pull qwen3:1.7b   # modèle par défaut de body-gateway (BRAIN_MODEL)
```

**Voix du stream dans OBS** ([ADR 005](decisions/005-avatar-twitch.md)) : ajouter une
**source navigateur** avec l'URL `https://obs.$DOMAIN/overlay?token=$OBS_TOKEN` et cocher
**« Contrôler l'audio via OBS »**. La voix devient une source du mixer, avec sous-titres.
Le corps `obs_01` n'est connecté (et ne reçoit la parole) que tant que cette source est ouverte.

---

## 8. Mettre à jour les services

```bash
# Toutes les images
docker compose pull
docker compose up -d

# Un service spécifique
docker compose pull bitterbot-gateway && docker compose up -d bitterbot-gateway
docker compose pull naoqi-bridge nao-core body-gateway ros2-bridge twitch-input obs-agent && docker compose up -d naoqi-bridge nao-core body-gateway ros2-bridge twitch-input obs-agent
```

Ou depuis Coolify : bouton **Redeploy** sur la resource.

---

## 9. Synchronisation cloud — rclone

Les dossiers `/opt/ia/sync/onedrive` et `/opt/ia/sync/gdrive` sont alimentés par `rclone` sur le VPS et montés en lecture seule dans le workspace de Bitterbot.

### Installation

```bash
curl https://rclone.org/install.sh | sudo bash
```

### Configuration (depuis ton poste local)

L'authentification OAuth nécessite un navigateur — faire la config en local puis copier sur le VPS.

**Google Drive :**
```bash
rclone config
# → n, name: gdrive, Storage: drive, laisser client_id vide, scope: 1
# → authentification navigateur, pas de Shared Drive
```

**OneDrive :**
```bash
rclone config
# → n, name: onedrive, Storage: onedrive, laisser client_id vide
# → authentification navigateur, type: onedrive, drive_id: 0
```

**Copier la config sur le VPS :**
```bash
scp ~/.config/rclone/rclone.conf $VPS_USER@$DOMAIN:/root/.config/rclone/rclone.conf
```

### Services systemd (synchronisation toutes les 15 min)

Créer `/etc/systemd/system/rclone-onedrive.service` :
```ini
[Unit]
Description=rclone sync — OneDrive → /opt/ia/sync/onedrive
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/rclone sync onedrive: /opt/ia/sync/onedrive \
    --log-file=/var/log/rclone-onedrive.log --log-level=INFO \
    --transfers=4 --checkers=8 --retries=3
```

Créer `/etc/systemd/system/rclone-onedrive.timer` :
```ini
[Unit]
Description=Sync OneDrive toutes les 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Unit=rclone-onedrive.service

[Install]
WantedBy=timers.target
```

Répéter pour `rclone-gdrive` (remplacer `onedrive:` par `gdrive:` et les chemins).

```bash
systemctl daemon-reload
systemctl enable --now rclone-onedrive.timer rclone-gdrive.timer
systemctl list-timers --all | grep rclone
```

---

## 10. Diagnostic

```bash
# État de tous les containers
docker compose ps

# Logs d'un service
docker logs ollama -f
docker logs body-gateway -f
docker logs nao-core -f
docker logs ros2-bridge -f
docker logs twitch-input -f
docker logs obs-agent -f

# Vérifier l'isolation réseau de Bitterbot (quand réactivé)
docker exec bitterbot-gateway curl -s --max-time 3 https://example.com
# Attendu : timeout ou "Could not resolve host"

# Vérifier que Bitterbot atteint Ollama (quand réactivé)
docker exec bitterbot-gateway curl -s http://ollama:11434/api/tags | head -c 100
```
