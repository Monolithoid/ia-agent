#!/usr/bin/env bash
# setup-ubuntu.sh — Installation du relais du NAO (ADR 006) sur le drone
# statique Ubuntu : le NAO parle en ws:// au drone, Caddy relaie vers le VPS
# en wss:// et ajoute le jeton. Automatise nao-relay/README.md, étapes 1 à 3.
#
# Par défaut, le relais ne démarre pas tout seul : on le lance à la main avec
# nao-relay/run.sh quand on en a besoin (PC non dédié). --service installe le
# service nao-relay, lancé au boot.
#
# Idempotent : on peut le relancer autant de fois qu'on veut ; il ne refait que
# ce qui manque et ne redémarre le service que si sa configuration a changé.
#
# Prérequis : le drone est installé (../setup-ubuntu.sh), son .env contient
# ROSBRIDGE_TOKEN et ROSBRIDGE_URL (ou DOMAIN).
#
# Usage (avec l'utilisateur du drone, pas en root) :
#   bash robotics/drones/static/onboard/nao-relay/setup-ubuntu.sh [--service]
#
# Sans terminal, NAO_RELAY_UPSTREAM, NAO_IP et NAO_RELAY_PORT peuvent être
# passés par l'environnement ; ils sont alors écrits dans le .env du drone.

set -euo pipefail

RELAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONBOARD_DIR="$(dirname "$RELAY_DIR")"
# shellcheck source=SCRIPTDIR/../setup-lib.sh
. "${ONBOARD_DIR}/setup-lib.sh"

ENV_FILE="${ONBOARD_DIR}/.env"
CADDYFILE="${RELAY_DIR}/Caddyfile"
UNIT_SRC="${RELAY_DIR}/nao-relay.service"
UNIT_DST="/etc/systemd/system/nao-relay.service"
LOCAL_NETS=(192.168.0.0/16 172.16.0.0/12 10.0.0.0/8)

CADDY_KEYRING="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
CADDY_LIST="/etc/apt/sources.list.d/caddy-stable.list"

OPT_SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --service) OPT_SERVICE=1 ;;
        -h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)         error "Option inconnue : $arg (voir --help)" ;;
    esac
done

is_placeholder() {
    local v="$1"
    [[ -z "$v" || "$v" == "change-me" || "$v" == *example.com* ]]
}

# ============================================================
# Configuration (.env du drone)
# ============================================================

# Déduit l'adresse https:// de rosbridge depuis ROSBRIDGE_URL ou DOMAIN
guess_upstream() {
    local url domain
    url="$(env_get "$ENV_FILE" ROSBRIDGE_URL)"
    domain="$(env_get "$ENV_FILE" DOMAIN)"
    if ! is_placeholder "$url"; then
        url="${url/#wss:\/\//https://}"
        url="${url/#ws:\/\//http://}"
        # Caddy veut un hôte, pas de chemin
        echo "$url" | sed -E 's#^(https?://[^/]+).*#\1#'
    elif ! is_placeholder "$domain"; then
        echo "https://rosbridge.${domain}"
    fi
}

setup_env() {
    section "Configuration (.env du drone)"
    [[ -f "$ENV_FILE" ]] \
        || error "$ENV_FILE absent : installer d'abord le drone (bash ${ONBOARD_DIR}/setup-ubuntu.sh)"
    is_placeholder "$(env_get "$ENV_FILE" ROSBRIDGE_TOKEN)" \
        && error "ROSBRIDGE_TOKEN à renseigner dans $ENV_FILE (même valeur que sur le VPS)"

    local key
    for key in NAO_RELAY_UPSTREAM NAO_IP NAO_RELAY_PORT; do
        if [[ -n "${!key:-}" ]]; then
            env_set "$ENV_FILE" "$key" "${!key}"
            log "$key pris dans l'environnement"
        fi
    done

    local upstream guess
    upstream="$(env_get "$ENV_FILE" NAO_RELAY_UPSTREAM)"
    if is_placeholder "$upstream"; then
        guess="$(guess_upstream)"
        upstream="$(ask "rosbridge du VPS en https:// (même hôte que ROSBRIDGE_URL)" "$guess")"
        [[ -n "$upstream" ]] || error "NAO_RELAY_UPSTREAM à renseigner dans $ENV_FILE"
        env_set "$ENV_FILE" NAO_RELAY_UPSTREAM "$upstream"
    fi
    [[ "$upstream" == https://* ]] \
        || warn "NAO_RELAY_UPSTREAM=$upstream n'est pas en https:// : le jeton partirait en clair"
    log "Relais vers : $upstream"

    if env_has "$ENV_FILE" NAO_IP && [[ -z "$(env_get "$ENV_FILE" NAO_IP)" ]]; then
        warn "NAO_IP= est vide dans $ENV_FILE : le relais refusera tout le monde."
        warn "Le commenter (#NAO_IP=) pour accepter le réseau local, ou mettre l'adresse du NAO."
    elif env_has "$ENV_FILE" NAO_IP; then
        log "Adresses acceptées : $(env_get "$ENV_FILE" NAO_IP)"
    else
        log "Adresses acceptées : tout le réseau local"
    fi
}

relay_port() {
    local p
    p="$(env_get "$ENV_FILE" NAO_RELAY_PORT)"
    echo "${p:-9090}"
}

# ============================================================
# Caddy
# ============================================================

install_caddy() {
    section "Caddy"
    if command -v caddy >/dev/null; then
        log "Caddy présent : $(caddy version | cut -d' ' -f1)"
    else
        local candidate
        candidate="$(apt-cache policy caddy 2>/dev/null | awk '/Candidate:/ { print $2 }')"
        if [[ -z "$candidate" || "$candidate" == "(none)" ]]; then
            # Ubuntu 22.04 : pas de caddy dans les dépôts, dépôt officiel
            info "Ajout du dépôt officiel de Caddy"
            sudo apt-get update -qq
            sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gpg
            if [[ ! -s "$CADDY_KEYRING" ]]; then
                curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
                    | sudo gpg --dearmor -o "$CADDY_KEYRING"
            fi
            if [[ ! -s "$CADDY_LIST" ]]; then
                curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
                    | sudo tee "$CADDY_LIST" >/dev/null
            fi
        fi
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
        log "Caddy installé : $(caddy version | cut -d' ' -f1)"
    fi

    # Le paquet lance un Caddy par défaut (port 80) : nao-relay le remplace
    if systemctl is-enabled -q caddy 2>/dev/null || systemctl is-active -q caddy 2>/dev/null; then
        sudo systemctl disable --now caddy
        log "Service caddy par défaut arrêté et désactivé"
    else
        log "Service caddy par défaut déjà désactivé"
    fi

    command -v curl >/dev/null || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl
}

# Vérifie le Caddyfile avec les valeurs du .env, comme le fera systemd
validate_caddyfile() {
    local vars=() key
    for key in NAO_RELAY_UPSTREAM ROSBRIDGE_TOKEN NAO_IP NAO_RELAY_PORT; do
        env_has "$ENV_FILE" "$key" && vars+=("$key=$(env_get "$ENV_FILE" "$key")")
    done
    if env -i HOME="$HOME" PATH="$PATH" "${vars[@]}" \
        caddy validate --adapter caddyfile --config "$CADDYFILE" >/dev/null 2>&1; then
        log "Caddyfile valide"
    else
        env -i HOME="$HOME" PATH="$PATH" "${vars[@]}" \
            caddy validate --adapter caddyfile --config "$CADDYFILE" || true
        error "Caddyfile refusé avec les valeurs du .env (voir ci-dessus)"
    fi
}

# ============================================================
# Service nao-relay
# ============================================================

setup_service() {
    section "Service nao-relay"

    # Le fichier du dépôt suppose User=pi et ~pi/ia-agent : on y met les vrais
    local tmp changed=0
    tmp="$(mktemp)"
    sed -e "s|^User=.*|User=${USER}|" \
        -e "s|/home/pi/ia-agent/robotics/drones/static/onboard|${ONBOARD_DIR}|g" \
        "$UNIT_SRC" > "$tmp"
    if install_if_changed "$tmp" "$UNIT_DST" sudo; then
        sudo systemctl daemon-reload
        changed=1
        log "Service installé : $UNIT_DST"
    else
        log "Service à jour : $UNIT_DST"
    fi
    rm -f "$tmp"

    sudo systemctl enable -q nao-relay
    if [[ $changed -eq 1 ]] || is_stale nao-relay "$ENV_FILE" "$CADDYFILE" "$UNIT_DST"; then
        sudo systemctl restart nao-relay
        log "Relais (re)démarré"
    else
        log "Relais déjà lancé, rien de changé"
    fi
}

# ============================================================
# Pare-feu (seulement s'il est actif)
# ============================================================

setup_firewall() {
    section "Pare-feu"
    if ! command -v ufw >/dev/null || ! sudo ufw status | grep -q "^Status: active"; then
        log "ufw inactif : rien à ouvrir"
        return
    fi
    local port sources src
    port="$(relay_port)"
    if env_has "$ENV_FILE" NAO_IP && [[ -n "$(env_get "$ENV_FILE" NAO_IP)" ]]; then
        read -ra sources <<< "$(env_get "$ENV_FILE" NAO_IP)"
    else
        sources=("${LOCAL_NETS[@]}")
    fi
    for src in "${sources[@]}"; do
        # ufw ignore une règle qui existe déjà
        sudo ufw allow from "$src" to any port "$port" proto tcp >/dev/null
    done
    log "Port $port ouvert à : ${sources[*]}"
}

# ============================================================
# Vérification
# ============================================================

check_relay() {
    section "Vérification"
    local port code temp_pid=""
    port="$(relay_port)"
    if ! systemctl is-active -q nao-relay 2>/dev/null; then
        # Pas de service : relais lancé le temps du test, puis arrêté
        bash "${RELAY_DIR}/run.sh" >/dev/null 2>&1 &
        temp_pid=$!
    fi
    for _ in 1 2 3 4 5; do
        code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
            -H "Connection: Upgrade" -H "Upgrade: websocket" \
            -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
            "http://127.0.0.1:${port}/" || true)"
        [[ "$code" == "000" ]] || break
        sleep 1
    done
    if [[ -n "$temp_pid" ]]; then
        kill "$temp_pid" 2>/dev/null || true
        wait "$temp_pid" 2>/dev/null || true
    fi
    case "$code" in
        101) log "Le relais et le VPS répondent (101 Switching Protocols)" ;;
        401) warn "401 : jeton refusé par le VPS (ROSBRIDGE_TOKEN différent de celui du VPS)" ;;
        403) warn "403 : adresse refusée par le relais (voir NAO_IP)" ;;
        502) warn "502 : le drone n'atteint pas NAO_RELAY_UPSTREAM" ;;
        000) warn "Le relais ne répond pas sur le port $port (port déjà pris ? lancer run.sh pour voir l'erreur)" ;;
        *)   warn "Réponse inattendue : $code" ;;
    esac
}

next_steps() {
    section "Côté NAO"
    local ip port
    ip="$(hostname -I | awk '{ print $1 }')"
    port="$(relay_port)"
    info "Le drone doit garder la même adresse : réservation DHCP sur la box pour ${ip:-cette machine}."
    info "Dans /home/nao/autostart.sh, supprimer « export ROSBRIDGE_TOKEN=... » et lancer :"
    echo "    python /home/nao/nao_ws_bridge.py --vps ws://${ip:-<IP du drone>}:${port}/ --provision &"
    if [[ $OPT_SERVICE -eq 1 ]]; then
        info "Journal du relais : journalctl -u nao-relay -f"
    else
        info "Lancer le relais (Ctrl+C pour l'arrêter) : bash ${RELAY_DIR}/run.sh"
        if [[ -f "$UNIT_DST" ]]; then
            warn "Un service nao-relay installé auparavant reste en place."
            warn "Pour l'enlever : sudo systemctl disable --now nao-relay"
        fi
    fi
}

# ============================================================
# MAIN
# ============================================================

main() {
    echo -e "${CYAN}Relais du NAO — installation Ubuntu${NC}  (${RELAY_DIR})"
    require_user_with_sudo
    require_ubuntu

    setup_env
    install_caddy
    validate_caddyfile
    if [[ $OPT_SERVICE -eq 1 ]]; then setup_service; fi
    setup_firewall
    check_relay
    next_steps
}

main
