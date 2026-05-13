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
ufw allow from NAO_PUBLIC_IP to any port 9090  # rosbridge ws:// — accès réservé à l'IP publique du NAO
ufw deny 11434
ufw deny 3000
ufw deny 5678
ufw enable
```

> Le port 19001 (Bitterbot) n'est pas exposé directement. Le port 9090 est restreint à l'IP publique du NAO (Python 2.7, OpenSSL trop vieux pour TLS 1.2) ; les clients WebSocket passent par `wss://rosbridge.$DOMAIN` via Traefik sur le port 443. Si l'IP du NAO est dynamique, la mettre à jour avec `ufw delete` + `ufw allow from NEW_IP to any port 9090`.

---

## 3. Configurer le projet dans Coolify

1. Coolify → **Projects** → créer le projet **"IA"**
2. **New Resource** → **Docker Compose** → pointer sur ce dépôt Git
3. Laisser le chemin du compose sur `docker-compose.yml` (racine)
4. Aller dans **Environment Variables** et ajouter toutes les variables ci-dessous

---

## 4. Variables d'environnement

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

**URI de callback à déclarer dans Google Cloud Console :**
```
https://chat.$DOMAIN/oauth/oidc/callback
```

---

## 5. Pipeline CI/CD des images Docker

Trois images sont buildées automatiquement par GitHub Actions et publiées sur Docker Hub. Le VPS fait uniquement `docker pull` — aucun build sur le serveur.

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
- Workflow **"Build & Push — automatic1111"** → **Run workflow**

Durées de build approximatives :
- Images robotiques (ROS 2) : 5–15 min chacune
- AUTOMATIC1111 (PyTorch CPU ~900 Mo) : **40–60 min au premier build**, puis ~5 min grâce au cache GHA

Images produites :
- `$DOCKERHUB_USERNAME/bitterbot:latest`
- `$DOCKERHUB_USERNAME/naoqi-bridge:latest`
- `$DOCKERHUB_USERNAME/nao-core:latest`
- `$DOCKERHUB_USERNAME/body-gateway:latest`
- `$DOCKERHUB_USERNAME/ros2-bridge:latest`
- `$DOCKERHUB_USERNAME/automatic1111:latest`

**Rebuilds automatiques :**
- Bitterbot : tous les lundis à 3h00 UTC
- Images robotiques : à chaque push sur `main` qui modifie `robotics/**`
- AUTOMATIC1111 : à chaque push sur `main` qui modifie `core/automatic1111/**`

---

## 6. Démarrer la stack

Dans Coolify → resource **"IA"** → bouton **Deploy** (ou **Start**).

Coolify exécute `docker compose pull && docker compose up -d`. Les données dans `/opt/ia/` sont préservées entre les redémarrages.

**Premier démarrage — télécharger les modèles Ollama :**
```bash
# Modèle LLM principal
docker exec -it ollama ollama pull qwen3:8b

# MoonDream — agent de vision pour analyser les planches de BD générées par A1111
docker exec -it ollama ollama pull moondream
```

**Premier démarrage — AUTOMATIC1111 :**  
Le container `automatic1111` télécharge automatiquement **DreamShaper 8** (~2,1 Go) depuis HuggingFace lors de son premier démarrage. Surveiller la progression :

```bash
docker logs automatic1111 -f
# Attendu : "[A1111] Téléchargement DreamShaper 8 (~2.1 Go)…"
# puis     : "[A1111] Démarrage (mode CPU)…"
# puis     : "Running on local URL:  http://0.0.0.0:7860"
```

Le téléchargement peut prendre 5–15 min selon la bande passante du VPS. Une fois terminé, le modèle est persisté dans `/opt/ia/automatic1111/models/` et les démarrages suivants sont immédiats.

---

## 7. Mettre à jour les services

```bash
# Toutes les images
docker compose pull
docker compose up -d

# Un service spécifique
docker compose pull bitterbot-gateway && docker compose up -d bitterbot-gateway
docker compose pull naoqi-bridge nao-core body-gateway ros2-bridge && docker compose up -d naoqi-bridge nao-core body-gateway ros2-bridge
docker compose pull automatic1111 && docker compose up -d automatic1111
```

Ou depuis Coolify : bouton **Redeploy** sur la resource.

---

## 8. Synchronisation cloud — rclone

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

## 9. Diagnostic

```bash
# État de tous les containers
docker compose ps

# Logs d'un service
docker logs ollama -f
docker logs body-gateway -f
docker logs nao-core -f
docker logs ros2-bridge -f
docker logs automatic1111 -f

# Vérifier que l'API A1111 répond
curl -s http://localhost:7860/sdapi/v1/sd-models | python3 -m json.tool | head -20
# Attendu : liste des checkpoints disponibles dont dreamshaper_8

# Générer une image de test (mode CPU — attendre 5–15 min)
curl -s http://localhost:7860/sdapi/v1/txt2img \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a cat","steps":10,"width":256,"height":256}' \
  | python3 -c "import sys,json,base64; open('/tmp/test.png','wb').write(base64.b64decode(json.load(sys.stdin)['images'][0]))"
ls -lh /tmp/test.png

# Vérifier que Open WebUI voit bien A1111
# → Admin Panel → Images → vérifie que "AUTOMATIC1111" est sélectionné
# → l'icône image doit apparaître dans la barre de saisie du chat

# Vérifier l'isolation réseau de Bitterbot (quand réactivé)
docker exec bitterbot-gateway curl -s --max-time 3 https://example.com
# Attendu : timeout ou "Could not resolve host"

# Vérifier que Bitterbot atteint Ollama (quand réactivé)
docker exec bitterbot-gateway curl -s http://ollama:11434/api/tags | head -c 100
```
