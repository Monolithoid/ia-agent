#!/usr/bin/env bash
# diag-nao-connection.sh — Diagnostique la chaîne de connexion NAO
#
# Vérifie chaque maillon :
#   NAO → rosbridge → /nao/status → naoqi-driver → POST /connected → naoqi-bridge /health
#
# Usage (sur le VPS, depuis la racine du repo) :
#   bash tests/diag-nao-connection.sh

set -uo pipefail

BRIDGE="http://localhost:8090"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

_ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
_fail() { echo -e "  ${RED}✗${NC}  $1"; }
_info() { echo -e "      ${CYAN}$1${NC}"; }
_head() { echo -e "\n${YELLOW}── $1${NC}"; }

echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   diag-nao-connection.sh — Diagnostic connexion NAO         ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

# ── 1. Conteneurs en cours ────────────────────────────────────────────────────
_head "1. Conteneurs Docker"
for svc in naoqi-bridge naoqi-driver ros2-bridge rosbridge-proxy; do
    status=$(docker inspect --format='{{.State.Status}}' "$svc" 2>/dev/null || echo "absent")
    if [ "$status" = "running" ]; then
        _ok "$svc → running"
    else
        _fail "$svc → $status"
    fi
done

# ── 2. naoqi-bridge : endpoint /connected présent ────────────────────────────
_head "2. naoqi-bridge — endpoint POST /connected"
code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
       -X POST "${BRIDGE}/connected" \
       -H "Content-Type: application/json" \
       -d '{"connected":false}' 2>/dev/null || echo "000")
if [ "$code" = "200" ]; then
    _ok "POST /connected répond HTTP 200 → code déployé"
else
    _fail "POST /connected → HTTP $code (image non reconstruite ?)"
    _info "Reconstruire : docker compose build naoqi-bridge && docker compose up -d naoqi-bridge"
fi

# ── 3. /nao/status publié sur le bus ROS ────────────────────────────────────
_head "3. Topic ROS /nao/status (heartbeat NAO → VPS)"
if docker exec naoqi-driver which ros2 >/dev/null 2>&1; then
    msg=$(docker exec naoqi-driver bash -c \
          "source /opt/ros/humble/setup.bash && \
           ROS_DOMAIN_ID=42 ros2 topic echo /nao/status std_msgs/msg/String \
           --once --timeout 10" 2>/dev/null | grep "data:" || true)
    if [ -n "$msg" ]; then
        _ok "/nao/status reçu : $msg"
    else
        _fail "/nao/status : aucun message en 10 s"
        _info "Le NAO n'envoie pas de heartbeat — vérifier nao_ws_bridge.py sur le robot"
        _info "et que rosbridge est joignable depuis le NAO (wss://rosbridge.DOMAIN)"
    fi
else
    _fail "ros2 introuvable dans naoqi-driver"
fi

# ── 4. naoqi-driver : logs récents autour de la connexion ────────────────────
_head "4. Logs naoqi-driver (20 dernières lignes)"
docker logs --tail 20 naoqi-driver 2>/dev/null | while IFS= read -r line; do
    _info "$line"
done

# ── 5. naoqi-bridge : injecter manuellement connected=true ────────────────────
_head "5. Injection manuelle connected=true → vérification /health"
inject=$(curl -s --max-time 5 -X POST "${BRIDGE}/connected" \
         -H "Content-Type: application/json" \
         -d '{"connected":true}' 2>/dev/null || echo "{}")
health=$(curl -s --max-time 5 "${BRIDGE}/health" 2>/dev/null || echo "{}")
conn=$(echo "$health" | python3 -c \
       "import json,sys; print(json.load(sys.stdin).get('connected','?'))" 2>/dev/null || echo "?")
if [ "$conn" = "True" ] || [ "$conn" = "true" ]; then
    _ok "/health connected=true après injection → endpoint opérationnel"
    _info "Si naoqi-driver ne l'appelle pas automatiquement, vérifier étapes 3 et 4"
else
    _fail "/health toujours connected=$conn après injection"
    _info "inject: $inject"
    _info "health: $health"
fi
# Remettre à false pour ne pas polluer l'état réel
curl -s --max-time 3 -X POST "${BRIDGE}/connected" \
     -H "Content-Type: application/json" \
     -d '{"connected":false}' >/dev/null 2>&1 || true

# ── 6. Logs naoqi-bridge (connexion NAO) ─────────────────────────────────────
_head "6. Logs naoqi-bridge (20 dernières lignes)"
docker logs --tail 20 naoqi-bridge 2>/dev/null | while IFS= read -r line; do
    _info "$line"
done

# ── Résumé ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}── Commandes de rebuild si nécessaire${NC}"
echo -e "  ${CYAN}docker compose build naoqi-bridge naoqi-driver${NC}"
echo -e "  ${CYAN}docker compose up -d naoqi-bridge naoqi-driver${NC}"
