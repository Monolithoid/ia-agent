#!/usr/bin/env bash
# setup-vps.sh — Préparation du VPS avant déploiement via Coolify
#
# Ce script couvre uniquement ce que Coolify ne gère pas :
#   - Arborescence des volumes host (/opt/ia/)
#   - Firewall UFW
#   - Configuration bitterbot.json (token gateway)
#   - rclone + services systemd (sync cloud)
#
# Ce que Coolify gère (ne pas dupliquer ici) :
#   - Installation Docker
#   - docker compose pull + up -d (bouton Deploy)
#   - Variables d'environnement (UI Coolify)
#   - Mises à jour (bouton Redeploy)
#
# Usage : sudo bash setup-vps.sh
# Prérequis : Ubuntu 22.04+ avec accès root, Coolify déjà installé

set -euo pipefail

# ============================================================
# CONFIGURATION
# ============================================================

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="/opt/ia"
BITTERBOT_CONFIG_SRC="${REPO_DIR}/core/bitterbot/bitterbot.json"
BITTERBOT_CONFIG_DST="${DATA_DIR}/bitterbot/config/bitterbot.json"

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
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} [${default}] : ")" value
        echo "${value:-$default}"
    else
        read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} : ")" value
        echo "$value"
    fi
}

# ============================================================
# ÉTAPE 1 — Arborescence des données
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
# ÉTAPE 2 — Firewall UFW
# ============================================================

configure_firewall() {
    section "Firewall UFW"
    if ! command -v ufw &>/dev/null; then
        apt-get install -y -q ufw
    fi

    ufw allow 22    comment "SSH"
    ufw allow 80    comment "HTTP (Coolify / Traefik)"
    ufw allow 443   comment "HTTPS (Coolify / Traefik)"
    ufw allow 8000  comment "Coolify UI"

    # body-gateway : autorisé depuis le subnet Docker bridge (chat-relay → host:8095)
    # Docker crée typiquement des bridges en 172.16-31.x.x ou 10.x.x.x selon le daemon
    ufw allow from 172.16.0.0/12 to any port 8095 comment "body-gateway depuis Docker bridge (ai-network)"
    ufw allow from 10.0.0.0/8    to any port 8095 comment "body-gateway depuis Docker bridge (ai-network, alt)"

    # rosbridge : uniquement depuis rosbridge-proxy (Docker bridge), qui vérifie le jeton.
    # Le NAO passe par Traefik (ws://rosbridge.DOMAIN, port 80), jamais en direct sur 9090.
    ufw allow from 172.16.0.0/12 to any port 9090 comment "rosbridge depuis rosbridge-proxy (Docker bridge)"
    ufw allow from 10.0.0.0/8    to any port 9090 comment "rosbridge depuis rosbridge-proxy (Docker bridge, alt)"

    # Ports internes — non exposés publiquement
    ufw deny 11434  comment "Ollama (reverse proxy uniquement)"
    ufw deny 3000   comment "Open WebUI (reverse proxy uniquement)"
    ufw deny 5678   comment "n8n (SSH tunnel uniquement)"
    ufw deny 9090   comment "rosbridge (via rosbridge-proxy + jeton uniquement)"
    ufw deny 8095   comment "body-gateway (Docker bridge uniquement, voir règles allow ci-dessus)"

    ufw --force enable
    log "UFW activé."
    ufw status verbose
}

# ============================================================
# ÉTAPE 3 — Configuration Bitterbot
# ============================================================

configure_bitterbot() {
    section "Configuration Bitterbot"

    if [[ -f "$BITTERBOT_CONFIG_DST" ]]; then
        warn "bitterbot.json déjà présent — conservé sans modification."
        return
    fi

    [[ -f "$BITTERBOT_CONFIG_SRC" ]] || error "Template introuvable : ${BITTERBOT_CONFIG_SRC}"

    # Générer le token gateway et l'afficher pour le saisir dans Coolify
    local token
    token=$(openssl rand -hex 32)

    cp "$BITTERBOT_CONFIG_SRC" "$BITTERBOT_CONFIG_DST"
    sed -i "s/CHANGE_THIS_BITTERBOT_TOKEN/${token}/g" "$BITTERBOT_CONFIG_DST"
    chown 1000:1000 "$BITTERBOT_CONFIG_DST"

    log "bitterbot.json configuré."
    echo ""
    echo -e "${YELLOW}══ ACTION REQUISE DANS COOLIFY ══${NC}"
    echo -e "  Variable : ${CYAN}BITTERBOT_GATEWAY_TOKEN${NC}"
    echo -e "  Valeur   : ${GREEN}${token}${NC}"
    echo ""
    warn "Copie cette valeur dans Coolify → Environment Variables avant de déployer."
    echo ""
    echo -e "  Variable : ${CYAN}ROSBRIDGE_TOKEN${NC} (à donner aussi au NAO : --token)"
    echo -e "  Valeur   : ${GREEN}$(openssl rand -hex 32)${NC}"
}

# ============================================================
# ÉTAPE 4 — rclone + services systemd (sync cloud optionnel)
# ============================================================

setup_rclone() {
    section "rclone — synchronisation cloud (optionnel)"

    local install_rclone
    install_rclone=$(ask "Installer rclone pour la sync OneDrive/GDrive ? (o/n)" "o")
    [[ "$install_rclone" =~ ^[Oo]$ ]] || { info "rclone ignoré."; return; }

    if ! command -v rclone &>/dev/null; then
        info "Installation de rclone…"
        curl -fsSL https://rclone.org/install.sh | bash
        log "rclone installé."
    else
        log "rclone déjà installé : $(rclone --version | head -1)"
    fi

    echo ""
    warn "La configuration OAuth de rclone nécessite un navigateur."
    warn "Effectue la config DEPUIS TON POSTE LOCAL, puis copie le fichier :"
    warn "  rclone config   # sur ta machine locale"
    warn "  scp ~/.config/rclone/rclone.conf root@VPS_IP:/root/.config/rclone/rclone.conf"
    echo ""

    local setup_timers
    setup_timers=$(ask "Créer les services systemd de sync (rclone.conf déjà copié ?) (o/n)" "n")
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
# MAIN
# ============================================================

main() {
    require_root

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║   Stack IA — Préparation VPS             ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
    echo ""

    create_directories
    configure_firewall
    configure_bitterbot
    setup_rclone

    echo ""
    echo -e "${GREEN}Préparation VPS terminée.${NC}"
    echo ""
    echo -e "${CYAN}Prochaines étapes dans Coolify :${NC}"
    echo "  1. Créer le projet "IA" → New Resource → Docker Compose → ce dépôt"
    echo "  2. Saisir toutes les variables d'environnement (voir README § 4)"
    echo "  3. Cliquer Deploy"
    echo ""
    echo -e "${CYAN}Après le premier déploiement Coolify :${NC}"
    echo "  → Télécharger un modèle Ollama :"
    echo "    docker exec -it ollama ollama pull qwen3:8b"
    echo ""
    echo "  → Déployer le bridge NAO (si robot présent) :"
    echo "    scp robotics/drones/nao/onboard/nao_ws_bridge.py nao@NAO_IP:/home/nao/"
    echo "    puis sur le NAO : python nao_ws_bridge.py --vps ws://rosbridge.DOMAIN/ --token \$ROSBRIDGE_TOKEN"
}

main "$@"
