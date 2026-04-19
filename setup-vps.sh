#!/usr/bin/env bash
# setup-vps.sh — Déploiement complet de la stack IA sur VPS Ubuntu
#
# Usage : sudo bash setup-vps.sh [--update]
#   --update  : pull les images et redémarre les containers (skip la config initiale)
#
# Prérequis : Ubuntu 22.04+ avec accès root

set -euo pipefail

# ============================================================
# CONFIGURATION
# ============================================================

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="/opt/ia"
ENV_FILE="${REPO_DIR}/.env"
BITTERBOT_CONFIG_SRC="${REPO_DIR}/core/bitterbot/bitterbot.json"
BITTERBOT_CONFIG_DST="${DATA_DIR}/bitterbot/config/bitterbot.json"
OLLAMA_DEFAULT_MODEL="qwen3:8b"

# Couleurs
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ============================================================
# HELPERS
# ============================================================

log()    { echo -e "${GREEN}[✓]${NC} $*"; }
info()   { echo -e "${CYAN}[→]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
error()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
section(){ echo -e "\n${CYAN}══ $* ══${NC}"; }

require_root() {
    [[ $EUID -eq 0 ]] || error "Ce script doit être exécuté en root : sudo bash $0"
}

ask() {
    local prompt="$1" var="$2" default="${3:-}"
    if [[ -n "$default" ]]; then
        read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} [${default}] : ")" value
        echo "${value:-$default}"
    else
        read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} : ")" value
        echo "$value"
    fi
}

ask_secret() {
    local prompt="$1"
    read -rsp "$(echo -e "${YELLOW}?${NC} ${prompt} : ")" value
    echo ""
    echo "$value"
}

gen_secret() { openssl rand -hex 32; }

# ============================================================
# MODE --update : pull + restart uniquement
# ============================================================

mode_update() {
    section "Mise à jour de la stack"
    cd "$REPO_DIR"
    info "Pull des nouvelles images Docker…"
    docker compose pull
    info "Redémarrage des services…"
    docker compose up -d --remove-orphans
    log "Stack mise à jour."
    echo ""
    docker compose ps
}

# ============================================================
# ÉTAPE 1 — Docker
# ============================================================

install_docker() {
    section "Docker"
    if command -v docker &>/dev/null; then
        log "Docker déjà installé : $(docker --version)"
        return
    fi
    info "Installation de Docker…"
    apt-get update -q
    apt-get install -y -q ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    log "Docker installé : $(docker --version)"
}

# ============================================================
# ÉTAPE 2 — Arborescence des données
# ============================================================

create_directories() {
    section "Arborescence /opt/ia"
    local dirs=(
        "${DATA_DIR}/ollama"
        "${DATA_DIR}/webui"
        "${DATA_DIR}/n8n"
        "${DATA_DIR}/chroma"
        "${DATA_DIR}/bitterbot/config"
        "${DATA_DIR}/bitterbot/workspace"
        "${DATA_DIR}/sync/onedrive"
        "${DATA_DIR}/sync/gdrive"
    )
    for d in "${dirs[@]}"; do
        mkdir -p "$d"
        log "Créé : $d"
    done
    # uid 1000 = utilisateur node dans le container Bitterbot
    chown -R 1000:1000 "${DATA_DIR}/bitterbot"
    log "Permissions bitterbot : 1000:1000"
}

# ============================================================
# ÉTAPE 3 — Firewall UFW
# ============================================================

configure_firewall() {
    section "Firewall UFW"
    if ! command -v ufw &>/dev/null; then
        apt-get install -y -q ufw
    fi

    ufw allow 22   comment "SSH"
    ufw allow 80   comment "HTTP (Coolify / Traefik)"
    ufw allow 443  comment "HTTPS (Coolify / Traefik)"
    ufw allow 9090 comment "rosbridge WebSocket (NAO)"

    # Ports internes — non exposés publiquement
    ufw deny 11434 comment "Ollama (via reverse proxy uniquement)"
    ufw deny 3000  comment "Open WebUI (via reverse proxy uniquement)"
    ufw deny 5678  comment "n8n (SSH tunnel uniquement)"

    ufw --force enable
    log "UFW activé."
    ufw status verbose
}

# ============================================================
# ÉTAPE 4 — Variables d'environnement (.env)
# ============================================================

configure_env() {
    section "Variables d'environnement"

    if [[ -f "$ENV_FILE" ]]; then
        warn "Fichier .env existant détecté : ${ENV_FILE}"
        local reuse
        reuse=$(ask "Réutiliser les valeurs existantes ? (o/n)" "" "o")
        if [[ "$reuse" =~ ^[Oo]$ ]]; then
            # shellcheck source=/dev/null
            set -a; source "$ENV_FILE"; set +a
            log "Variables chargées depuis .env"
            return
        fi
    fi

    echo ""
    info "Génération des secrets aléatoires…"

    local ollama_api_keys webui_secret_key bitterbot_gateway_token gateway_api_key
    local n8n_password oauth_client_id oauth_client_secret webui_admin_email

    ollama_api_keys=$(gen_secret)
    webui_secret_key=$(gen_secret)
    bitterbot_gateway_token=$(gen_secret)
    gateway_api_key=$(gen_secret)

    echo ""
    info "Les valeurs suivantes doivent être saisies manuellement :"
    echo ""
    webui_admin_email=$(ask "WEBUI_ADMIN_EMAIL (ton adresse email)")
    n8n_password=$(ask_secret "N8N_BASIC_AUTH_PASSWORD (mot de passe fort)")
    echo ""
    echo ""
    warn "Pour OAUTH_CLIENT_ID et OAUTH_CLIENT_SECRET :"
    warn "  → console.cloud.google.com → APIs & Services → Credentials → OAuth 2.0"
    warn "  → URI de callback : https://chat.monolithoid.com/oauth/oidc/callback"
    echo ""
    oauth_client_id=$(ask "OAUTH_CLIENT_ID")
    oauth_client_secret=$(ask_secret "OAUTH_CLIENT_SECRET")
    echo ""

    cat > "$ENV_FILE" <<EOF
# Stack IA — variables d'environnement
# Généré par setup-vps.sh le $(date -u +"%Y-%m-%d %H:%M UTC")
# NE PAS COMMITER CE FICHIER

OLLAMA_API_KEYS=${ollama_api_keys}
WEBUI_SECRET_KEY=${webui_secret_key}
WEBUI_ADMIN_EMAIL=${webui_admin_email}
OAUTH_CLIENT_ID=${oauth_client_id}
OAUTH_CLIENT_SECRET=${oauth_client_secret}
N8N_BASIC_AUTH_PASSWORD=${n8n_password}
BITTERBOT_GATEWAY_TOKEN=${bitterbot_gateway_token}
GATEWAY_API_KEY=${gateway_api_key}
EOF

    chmod 600 "$ENV_FILE"
    log "Fichier .env créé : ${ENV_FILE}"
    echo ""
    warn "Si tu utilises Coolify, copie ces valeurs dans Environment Variables de la resource."
    # shellcheck source=/dev/null
    set -a; source "$ENV_FILE"; set +a
}

# ============================================================
# ÉTAPE 5 — Config Bitterbot
# ============================================================

configure_bitterbot() {
    section "Configuration Bitterbot"

    if [[ -f "$BITTERBOT_CONFIG_DST" ]]; then
        warn "bitterbot.json déjà présent — conservé sans modification."
        return
    fi

    if [[ ! -f "$BITTERBOT_CONFIG_SRC" ]]; then
        error "Template introuvable : ${BITTERBOT_CONFIG_SRC}"
    fi

    cp "$BITTERBOT_CONFIG_SRC" "$BITTERBOT_CONFIG_DST"

    # Remplacer le placeholder par le vrai token
    local token="${BITTERBOT_GATEWAY_TOKEN:-}"
    if [[ -z "$token" ]]; then
        warn "BITTERBOT_GATEWAY_TOKEN non défini — le placeholder n'est pas remplacé."
        warn "Édite manuellement : ${BITTERBOT_CONFIG_DST}"
    else
        sed -i "s/CHANGE_THIS_BITTERBOT_TOKEN/${token}/g" "$BITTERBOT_CONFIG_DST"
        chown 1000:1000 "$BITTERBOT_CONFIG_DST"
        log "bitterbot.json configuré avec le token gateway."
    fi
}

# ============================================================
# ÉTAPE 6 — rclone + services systemd (sync cloud optionnel)
# ============================================================

setup_rclone() {
    section "rclone — synchronisation cloud (optionnel)"

    local install_rclone
    install_rclone=$(ask "Installer rclone pour la sync OneDrive/GDrive ? (o/n)" "" "o")
    [[ "$install_rclone" =~ ^[Oo]$ ]] || { info "rclone ignoré."; return; }

    if ! command -v rclone &>/dev/null; then
        info "Installation de rclone…"
        curl -fsSL https://rclone.org/install.sh | bash
        log "rclone installé."
    else
        log "rclone déjà installé : $(rclone --version | head -1)"
    fi

    warn "La configuration OAuth de rclone nécessite un navigateur."
    warn "Effectue la config DEPUIS TON POSTE LOCAL, puis copie le fichier :"
    warn "  rclone config   # sur ta machine locale"
    warn "  scp ~/.config/rclone/rclone.conf root@VPS_IP:/root/.config/rclone/rclone.conf"
    echo ""

    local setup_timers
    setup_timers=$(ask "Créer les services systemd de sync (nécessite rclone.conf déjà copié) ? (o/n)" "" "n")
    [[ "$setup_timers" =~ ^[Oo]$ ]] || return

    _create_rclone_service "onedrive" "onedrive:" "${DATA_DIR}/sync/onedrive"
    _create_rclone_service "gdrive"   "gdrive:"   "${DATA_DIR}/sync/gdrive"

    systemctl daemon-reload
    systemctl enable --now rclone-onedrive.timer rclone-gdrive.timer
    log "Timers rclone activés (sync toutes les 15 min)."
    systemctl list-timers --all | grep rclone
}

_create_rclone_service() {
    local name="$1" remote="$2" dest="$3"

    cat > "/etc/systemd/system/rclone-${name}.service" <<EOF
[Unit]
Description=rclone sync — ${remote} → ${dest}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/rclone sync ${remote} ${dest} \
    --log-file=/var/log/rclone-${name}.log --log-level=INFO \
    --transfers=4 --checkers=8 --retries=3
EOF

    cat > "/etc/systemd/system/rclone-${name}.timer" <<EOF
[Unit]
Description=Sync ${name} toutes les 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Unit=rclone-${name}.service

[Install]
WantedBy=timers.target
EOF

    log "Service systemd créé : rclone-${name}"
}

# ============================================================
# ÉTAPE 7 — Pull images et démarrage
# ============================================================

deploy_stack() {
    section "Déploiement Docker Compose"
    cd "$REPO_DIR"

    info "Pull des images Docker Hub…"
    docker compose pull

    info "Démarrage de la stack…"
    docker compose up -d --remove-orphans

    log "Stack démarrée."
    echo ""
    docker compose ps
}

# ============================================================
# ÉTAPE 8 — Téléchargement du modèle Ollama
# ============================================================

pull_ollama_model() {
    section "Modèle Ollama"

    local model
    model=$(ask "Modèle à télécharger" "" "$OLLAMA_DEFAULT_MODEL")

    info "Attente du démarrage d'Ollama…"
    local attempts=0
    until docker exec ollama ollama list &>/dev/null 2>&1; do
        ((attempts++))
        [[ $attempts -gt 30 ]] && error "Ollama ne répond pas après 60 secondes."
        sleep 2
    done

    info "Téléchargement du modèle ${model}…"
    docker exec ollama ollama pull "$model"
    log "Modèle ${model} disponible."
}

# ============================================================
# ÉTAPE 9 — Vérification finale
# ============================================================

verify() {
    section "Vérification"

    echo ""
    info "État des containers :"
    docker compose ps

    echo ""
    info "Test endpoint Ollama (interne)…"
    if docker exec ollama ollama list &>/dev/null 2>&1; then
        log "Ollama répond."
    else
        warn "Ollama ne répond pas encore — attendre quelques secondes."
    fi

    echo ""
    info "Test isolation réseau Bitterbot…"
    if docker exec bitterbot-gateway curl -s --max-time 3 https://example.com 2>&1 | grep -q "Could not resolve\|timed out\|Failed"; then
        log "Bitterbot : isolation réseau OK (pas d'accès internet)."
    else
        warn "Vérifier l'isolation réseau de bitterbot-net."
    fi

    echo ""
    log "Déploiement terminé."
    echo ""
    echo -e "${CYAN}Accès aux services :${NC}"
    echo "  Open WebUI  → https://chat.monolithoid.com"
    echo "  API Ollama  → https://ollama.monolithoid.com  (Bearer \$OLLAMA_API_KEYS)"
    echo "  n8n         → ssh -L 5678:localhost:5678 user@monolithoid.com"
    echo "  Bitterbot   → ssh -L 19001:localhost:19001 user@monolithoid.com"
    echo "  nao-gateway → ssh -L 8001:localhost:8001 user@monolithoid.com"
    echo ""
    echo -e "${YELLOW}Prochaines étapes manuelles :${NC}"
    echo "  1. Configurer le projet dans Coolify (Docker Compose → ce dépôt)"
    echo "  2. Copier les variables du fichier .env dans Coolify → Environment Variables"
    echo "  3. Déclencher les builds GitHub Actions (bitterbot + robotics)"
    echo "  4. Si robot NAO : scp robotics/nao/nao_ws_bridge.py nao@NAO_IP:/home/nao/"
}

# ============================================================
# MAIN
# ============================================================

main() {
    require_root

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║   Stack IA — Déploiement VPS             ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
    echo ""

    if [[ "${1:-}" == "--update" ]]; then
        mode_update
        return
    fi

    install_docker
    create_directories
    configure_firewall
    configure_env
    configure_bitterbot
    setup_rclone
    deploy_stack
    pull_ollama_model
    verify
}

main "$@"
