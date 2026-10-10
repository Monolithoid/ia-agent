#!/usr/bin/env bash
# run.sh — Lance le relais du NAO au premier plan, sans service systemd
# (Ctrl+C pour l'arrêter). Lit sa configuration dans le .env du drone, comme
# le ferait nao-relay.service.
#
# Usage : bash robotics/drones/static/onboard/nao-relay/run.sh

set -euo pipefail

RELAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ONBOARD_DIR="$(dirname "$RELAY_DIR")"
# shellcheck source=SCRIPTDIR/../setup-lib.sh
. "${ONBOARD_DIR}/setup-lib.sh"

ENV_FILE="${ONBOARD_DIR}/.env"
[[ -f "$ENV_FILE" ]] || error "$ENV_FILE absent : lancer d'abord nao-relay/setup-ubuntu.sh"
command -v caddy >/dev/null || error "Caddy absent : lancer d'abord nao-relay/setup-ubuntu.sh"

# Seules les valeurs du .env comptent (une ligne NAO_IP= vide refuse tout)
for key in NAO_RELAY_UPSTREAM ROSBRIDGE_TOKEN NAO_IP NAO_RELAY_PORT; do
    unset "$key"
    if env_has "$ENV_FILE" "$key"; then
        export "$key=$(env_get "$ENV_FILE" "$key")"
    fi
done

exec caddy run --adapter caddyfile --config "${RELAY_DIR}/Caddyfile"
