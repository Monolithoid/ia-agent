# Coolify & VPS

Déploiement de la stack sur un VPS Ubuntu via Docker Compose géré par Coolify.

---

## 1. Prérequis sur le VPS

Se connecter en SSH et créer l'arborescence de données persistantes :

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

Un script `setup-vps.sh` à la racine du dépôt automatise ces étapes.

---

## 2. Firewall (UFW)

```bash
ufw allow 22
ufw allow 80
ufw allow 443
ufw allow 9090    # rosbridge WebSocket — NAO se connecte depuis l'extérieur
ufw deny 11434
ufw deny 3000
ufw deny 5678
ufw enable
```

> Les ports 8001 (nao-gateway) et 19001 (Bitterbot) sont liés à `127.0.0.1` dans le compose — aucune règle UFW nécessaire.

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
| `GATEWAY_API_KEY` | `openssl rand -hex 32` — clé `X-API-Key` pour les appels n8n → nao-gateway |

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

Chaque build prend 5–15 min. Les images produites :
- `$DOCKERHUB_USERNAME/bitterbot:latest`
- `$DOCKERHUB_USERNAME/nao-gateway:latest`
- `$DOCKERHUB_USERNAME/ros2-bridge:latest`

**Rebuilds automatiques :**
- Bitterbot : tous les lundis à 3h00 UTC
- Images robotiques : à chaque push sur `main` qui modifie `robotics/**`

---

## 6. Démarrer la stack

Dans Coolify → resource **"IA"** → bouton **Deploy** (ou **Start**).

Coolify exécute `docker compose pull && docker compose up -d`. Les données dans `/opt/ia/` sont préservées entre les redémarrages.

**Premier démarrage — télécharger un modèle LLM :**
```bash
docker exec -it ollama ollama pull qwen3:8b
```

---

## 7. Mettre à jour les services

```bash
# Toutes les images
docker compose pull
docker compose up -d

# Un service spécifique
docker compose pull bitterbot-gateway && docker compose up -d bitterbot-gateway
docker compose pull nao-gateway ros2-bridge && docker compose up -d nao-gateway ros2-bridge
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
docker logs nao-gateway -f
docker logs ros2-bridge -f

# Vérifier l'isolation réseau de Bitterbot
docker exec bitterbot-gateway curl -s --max-time 3 https://example.com
# Attendu : timeout ou "Could not resolve host"

# Vérifier que Bitterbot atteint Ollama
docker exec bitterbot-gateway curl -s http://ollama:11434/api/tags | head -c 100
```
