#!/usr/bin/env bash
# test-chain.sh — Teste chaque maillon de la chaîne WebUI → NAO
#
# Usage (sur le VPS, depuis la racine du repo) :
#   bash tests/test-chain.sh
#
# Prérequis : docker, curl disponibles.
# NAO peut être éteint — naoqi-bridge tourne alors en mock mode.
#
# Chaîne testée :
#
#   open-webui (nao_pipe.py)
#     │  POST http://chat-relay:8095/chat   [ai-network]
#     ▼
#   chat-relay (Caddy)
#     │  reverse-proxy → host-gateway:8095
#     ▼
#   body-gateway  (host network, port 8095)
#     │  POST http://localhost:11434/v1/chat/completions
#     ▼
#   ollama  (ai-network, port 11434 exposé sur le host)
#
#   body-gateway → publie /bodies/nao_01/actuators/* (ROS2 DDS domain 42)
#     ▼
#   nao-core  (host network, DDS 42 — naoqi-driver + cognitive-renderer + motion-engine)
#     │  POST http://localhost:8090/say|motion|leds|…
#     ▼
#   naoqi-bridge  (ai-network, port 127.0.0.1:8090 exposé sur le host)
#     │  qi SDK → tcp://NAO_IP:9559   [MOCK si NAO éteint]
#     ▼
#   NAO (éteint → réponse mock)
#
#   Maillon 9 — drone statique simulé (tests/fake-static-drone.py) :
#   faux messages rosbridge /drones/static_01/* → nao-core/static-driver
#   → whisper-asr → body-gateway → ollama → /drones/static_01/audio_out

set -uo pipefail

# ─── Couleurs ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS=0
FAIL=0

_pass() { echo -e "  ${GREEN}✓ PASS${NC}  $1"; PASS=$((PASS + 1)); }
_fail() { echo -e "  ${RED}✗ FAIL${NC}  $1"; FAIL=$((FAIL + 1)); }
_info() { echo -e "        ${CYAN}$1${NC}"; }
_head() { echo -e "\n${YELLOW}┌─ MAILLON $1 ─ $2${NC}"; }
_skip() { echo -e "  ${BLUE}⊘ SKIP${NC}  $1"; }

# Teste une commande ; $1 = description, reste = commande
_check() {
    local desc="$1"; shift
    local out rc
    out=$(eval "$@" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ]; then
        _pass "$desc"
        [ -n "$out" ] && _info "$(echo "$out" | head -3)"
    else
        _fail "$desc"
        [ -n "$out" ] && _info "$(echo "$out" | head -5)"
    fi
}

# ─── Résolution de conteneur robuste aux suffixes Coolify ────────────────────
# Coolify renomme les conteneurs (préfixe projet, suffixe aléatoire).
# Le label com.docker.compose.service=<nom> est lui toujours stable.
# Les noms DNS à l'intérieur des réseaux Docker restent les noms de service
# définis dans le compose — seul le nom du conteneur change.

_container() {
    # Retourne le nom du premier conteneur running pour ce service.
    docker ps \
        --filter "label=com.docker.compose.service=$1" \
        --filter "status=running" \
        --format "{{.Names}}" | head -1
}

_container_running() {
    [ -n "$(_container "$1")" ]
}

echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   test-chain.sh — WebUI → NAO (NAO peut être éteint)        ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 0 — Conteneurs Docker en cours d'exécution
# ─────────────────────────────────────────────────────────────────────────────
_head 0 "Conteneurs Docker"

for svc in open-webui chat-relay body-gateway naoqi-bridge nao-core; do
    cname=$(_container "$svc")
    if [ -n "$cname" ]; then
        _pass "service $svc : running  (→ $cname)"
    else
        _fail "service $svc : aucun conteneur running trouvé"
    fi
done

# Optionnel (ne bloque pas les tests suivants)
for svc in ollama chromadb n8n whisper-asr ros2-bridge; do
    cname=$(_container "$svc")
    if [ -n "$cname" ]; then
        _pass "service $svc : running  (→ $cname)"
    else
        _skip "service $svc : absent (optionnel pour ce test)"
    fi
done

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 1 — open-webui → chat-relay (réseau ai-network)
# ─────────────────────────────────────────────────────────────────────────────
_head 1 "open-webui → chat-relay:8095 (ai-network)"

echo -e "  ${BLUE}→${NC} Envoie POST /chat depuis le conteneur open-webui vers chat-relay:8095"

# Résolution du conteneur open-webui (nom variable selon Coolify)
_C_WEBUI=$(_container open-webui)

if [ -z "$_C_WEBUI" ]; then
    _fail "open-webui introuvable — maillons 1 et 2 ignorés"
else
    # Vérifie d'abord que le réseau DNS fonctionne
    _check "résolution DNS chat-relay depuis open-webui ($_C_WEBUI)" \
        "docker exec $_C_WEBUI getent hosts chat-relay"

    # Vérifie que chat-relay répond (toute réponse HTTP = connexion établie)
    # On accepte 2xx/4xx/5xx — une connexion refusée donnerait "000".
    _check "TCP chat-relay:8095 joignable depuis open-webui (toute réponse HTTP)" \
        "docker exec $_C_WEBUI sh -c 'curl -s --max-time 5 -o /dev/null -w \"%{http_code}\" http://chat-relay:8095/chat -X POST -H \"Content-Type: application/json\" -d \"{\\\"text\\\":\\\"\\\",\\\"robot_id\\\":\\\"nao_01\\\"}\"' | grep -E '^[2-5][0-9][0-9]$'"
fi

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 2 — chat-relay → body-gateway (host-gateway:8095)
# ─────────────────────────────────────────────────────────────────────────────
_head 2 "chat-relay → gateway hôte :8095 (body-gateway)"

echo -e "  ${BLUE}→${NC} Caddy dans chat-relay doit atteindre l'hôte sur :8095"

# Sans -f : body-gateway retourne 400 {"error":"missing text"} pour texte vide.
# curl -f supprimerait le corps sur 4xx et ferait échouer le test à tort.
_check "body-gateway /chat répond depuis le host (port 8095)" \
    "curl -s --max-time 5 -X POST http://localhost:8095/chat \
     -H 'Content-Type: application/json' \
     -d '{\"text\":\"\",\"robot_id\":\"nao_01\"}' | grep -i 'missing\|error\|speech'"

# Affiche les logs de démarrage de chat-relay pour diagnostic
_C_RELAY=$(_container chat-relay)
if [ -n "$_C_RELAY" ]; then
    _info "Logs de démarrage chat-relay (→ $_C_RELAY) :"
    docker logs --tail=10 "$_C_RELAY" 2>&1 | while IFS= read -r line; do _info "  $line"; done
    _check "chat-relay a détecté le gateway hôte" \
        "docker logs --tail=20 $_C_RELAY 2>&1 | grep 'gateway hôte détecté'"
fi

if [ -n "${_C_WEBUI:-}" ]; then
    # Récupère la réponse brute pour afficher un diagnostic utile en cas d'échec
    _check "Caddy chat-relay proxie vers body-gateway (depuis open-webui)" \
        "resp=\$(docker exec $_C_WEBUI sh -c 'curl -s --max-time 5 -X POST http://chat-relay:8095/chat \
         -H \"Content-Type: application/json\" \
         -d \"{\\\"text\\\":\\\"\\\",\\\"robot_id\\\":\\\"nao_01\\\"}\"') \
         && echo \"\$resp\" | grep -i 'missing\|error\|speech' \
         || { echo \"Réponse inattendue : \$resp\"; false; }"
fi

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 3 — body-gateway → Ollama (localhost:11434)
# ─────────────────────────────────────────────────────────────────────────────
_head 3 "body-gateway → Ollama (localhost:11434)"

echo -e "  ${BLUE}→${NC} body-gateway (mode host) appelle http://localhost:11434"

_check "Ollama /api/tags répond" \
    "curl -sf --max-time 10 http://localhost:11434/api/tags | grep -q 'models'"

_check "Ollama liste le modèle qwen2.5:7b" \
    "curl -sf --max-time 10 http://localhost:11434/api/tags | grep -q 'qwen2.5:7b'"

_check "Ollama /v1/chat/completions (endpoint utilisé par body-gateway)" \
    "curl -sf --max-time 60 -X POST http://localhost:11434/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{\"model\":\"qwen2.5:7b\",\"messages\":[{\"role\":\"user\",\"content\":\"Réponds uniquement avec le mot: ok\"}],\"stream\":false}' \
     | grep -q 'choices'"

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 3b — body-gateway /chat complet (inférence réelle)
# ─────────────────────────────────────────────────────────────────────────────
_head "3b" "Inférence complète body-gateway → Ollama → réponse JSON"

echo -e "  ${BLUE}→${NC} POST /chat avec un vrai texte — body-gateway doit retourner speech+actions"

_check "POST /chat {text: 'bonjour'} → JSON avec 'speech'" \
    "curl -sf --max-time 60 -X POST http://localhost:8095/chat \
     -H 'Content-Type: application/json' \
     -d '{\"text\":\"bonjour\",\"robot_id\":\"nao_01\"}' \
     | grep -q 'speech'"

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 4 — body-gateway → ROS2 bus (DDS domain 42)
# ─────────────────────────────────────────────────────────────────────────────
_head 4 "body-gateway → ROS2 DDS domain 42"

echo -e "  ${BLUE}→${NC} Vérifie que les topics ROS2 sont publiés"

_C_CORE=$(_container nao-core)
if [ -n "$_C_CORE" ]; then
    _check "ros2 topic list donne /cognitive_state et /bodies/* (→ $_C_CORE)" \
        "docker exec $_C_CORE bash -c \
         'source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 \
          timeout 5 ros2 topic list 2>/dev/null' \
         | grep -E '/cognitive_state|/bodies'"

    _check "/cognitive_state a un publisher actif" \
        "docker exec $_C_CORE bash -c \
         'source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 \
          timeout 5 ros2 topic info /cognitive_state 2>/dev/null' \
         | grep -i 'publisher'"

    _check "/motion_engine/command souscrit par motion-engine" \
        "docker exec $_C_CORE bash -c \
         'source /opt/ros/humble/setup.bash && ROS_DOMAIN_ID=42 \
          timeout 5 ros2 topic info /motion_engine/command 2>/dev/null' \
         | grep -i 'subscription'"
else
    _skip "nao-core non disponible — test ROS2 ignoré"
fi

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 5 — nao-core → naoqi-bridge (localhost:8090)
# ─────────────────────────────────────────────────────────────────────────────
_head 5 "nao-core → naoqi-bridge (localhost:8090)"

echo -e "  ${BLUE}→${NC} naoqi-bridge est exposé sur 127.0.0.1:8090 côté host"

_check "naoqi-bridge /health répond" \
    "curl -sf --max-time 5 http://localhost:8090/health"

_check "naoqi-bridge /health : ok=true" \
    "curl -sf --max-time 5 http://localhost:8090/health | grep -q '\"ok\": *true'"

_check "naoqi-bridge /say répond (doit être ok=true en mock ou réel)" \
    "curl -sf --max-time 5 -X POST http://localhost:8090/say \
     -H 'Content-Type: application/json' \
     -d '{\"text\":\"test maillon 5\"}' | grep -q 'true'"

_check "naoqi-bridge /leds répond (mock)" \
    "curl -sf --max-time 5 -X POST http://localhost:8090/leds \
     -H 'Content-Type: application/json' \
     -d '{\"group\":\"AllLeds\",\"r\":0.0,\"g\":1.0,\"b\":0.0,\"duration\":0.5}' \
     | grep -q 'true'"

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 6 — naoqi-bridge mode mock (NAO éteint)
# ─────────────────────────────────────────────────────────────────────────────
_head 6 "naoqi-bridge (NAO éteint → mock mode attendu)"

echo -e "  ${BLUE}→${NC} ok doit toujours être true ; connected reflète l'état réel de NAO"

_BRIDGE_CONNECTED=$(curl -sf --max-time 5 http://localhost:8090/health \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(str(d.get('connected',False)).lower())" 2>/dev/null || echo "unknown")

_check "naoqi-bridge ok=true (service opérationnel)" \
    "curl -sf --max-time 5 http://localhost:8090/health \
     | python3 -c \
     \"import json,sys; d=json.load(sys.stdin); assert d.get('ok')==True, d; \
       s='ok={ok}, connected={c}'.format(ok=d['ok'],c=d.get('connected',False)); \
       print(s)\""

_C_BRIDGE=$(_container naoqi-bridge)
if [ -n "$_C_BRIDGE" ]; then
    if [ "$_BRIDGE_CONNECTED" = "true" ]; then
        _info "NAO connecté → mode réel (pas de MOCK attendu dans les logs)"
        _check "naoqi-bridge logs : connexion qi établie" \
            "docker logs --tail=50 $_C_BRIDGE 2>&1 | grep -iE 'connect|session|nao'"
    else
        _info "NAO non connecté → mock mode attendu dans les logs"
        _check "naoqi-bridge logs contiennent MOCK (→ $_C_BRIDGE)" \
            "docker logs --tail=50 $_C_BRIDGE 2>&1 | grep -i 'mock\|MOCK'"
    fi
else
    _skip "naoqi-bridge introuvable — vérification des logs ignorée"
fi

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 8 — Pipe WebUI → body-gateway (__ping__ sans Ollama)
# ─────────────────────────────────────────────────────────────────────────────
_head 8 "Pipe Open WebUI → body-gateway (message __ping__)"

echo -e "  ${BLUE}→${NC} Envoie __ping__ via /api/chat/completions — vérifie que le pipe atteint body-gateway"

# Lecture des variables d'environnement (les mêmes que body-gateway utilise)
_WEBUI_URL="${WEBUI_URL:-http://localhost:3000}"
_WEBUI_KEY="${WEBUI_API_KEY:-}"
_PIPE_MODEL="${WEBUI_PIPE_MODEL:-nao_brain}"

if [ -z "$_WEBUI_KEY" ]; then
    _skip "WEBUI_API_KEY non défini — test pipe ignoré (définir la variable d'env pour l'activer)"
else
    # Test 1 : body-gateway /ping accessible directement depuis le host
    _check "body-gateway GET /ping répond ok=true (host:8095)" \
        "curl -sf --max-time 5 http://localhost:8095/ping | python3 -c \
         \"import json,sys; d=json.load(sys.stdin); assert d.get('ok') == True, d; print(d)\""

    # Test 2 : le pipe répond via l'API WebUI avec __ping__
    _check "Pipe $_PIPE_MODEL : __ping__ → body-gateway joignable" \
        "resp=\$(curl -sf --max-time 15 -X POST '$_WEBUI_URL/api/chat/completions' \
             -H 'Authorization: Bearer $_WEBUI_KEY' \
             -H 'Content-Type: application/json' \
             -d '{\"model\":\"$_PIPE_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"__ping__\"}],\"stream\":false}') \
         && echo \"\$resp\" | python3 -c \
             \"import json,sys; d=json.load(sys.stdin); \
               content=d['choices'][0]['message']['content']; \
               assert 'body-gateway joignable' in content or 'opérationnel' in content, repr(content); \
               print(content)\""
fi

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 9 — Drone statique simulé via rosbridge (ADR 004)
# ─────────────────────────────────────────────────────────────────────────────
_head 9 "Drone statique simulé → rosbridge → static-driver → body-gateway"

echo -e "  ${BLUE}→${NC} Faux drone ${STATIC_DRONE_ID:-static_01} : status, image, audio (angle -30°), parole"
_info "Si un vrai drone ${STATIC_DRONE_ID:-static_01} est branché, il prononcera les phrases du test."

if _container_running "ros2-bridge" && _container_running "nao-core"; then
    # Connexion directe à rosbridge sur le host (port 9090, non filtré en local).
    _static_out=$(python3 "$(dirname "$0")/fake-static-drone.py" \
        --url "ws://localhost:9090" \
        --id "${STATIC_DRONE_ID:-static_01}" --nao "${NAO_ROBOT_ID:-nao_01}" 2>&1)
    _static_rc=$?
    echo "$_static_out" | sed 's/^/  /'
    if [ "$_static_rc" -eq 0 ]; then
        _pass "chemin drone statique de bout en bout"
    else
        _fail "chemin drone statique : $_static_rc étape(s) en échec"
    fi
else
    _skip "ros2-bridge ou nao-core absent — drone statique non testé"
fi

# ─────────────────────────────────────────────────────────────────────────────
# MAILLON 7 — n8n accessible (bonus — n8n n'est pas dans la chaîne directe)
# ─────────────────────────────────────────────────────────────────────────────
_head 7 "n8n (bonus — workflow orchestration, hors chaîne directe)"

echo -e "  ${BLUE}→${NC} n8n est dans ai-network mais n'est pas dans la chaîne chat directe"

# n8n n'a pas de ports: dans le compose — il n'est pas exposé sur le host.
# On le teste depuis open-webui qui est dans le même ai-network.
# n8n a l'auth basique active → 401 est une réponse valide (service up).
if _container_running "n8n" && [ -n "${_C_WEBUI:-}" ]; then
    _check "n8n répond sur ai-network (→ $(_container n8n))" \
        "docker exec $_C_WEBUI curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://n8n:5678/ | grep -E '200|301|302|401'"
elif _container_running "n8n"; then
    _skip "n8n running mais open-webui introuvable pour tester depuis ai-network"
else
    _skip "n8n non démarré"
fi

# ─────────────────────────────────────────────────────────────────────────────
# RÉSUMÉ
# ─────────────────────────────────────────────────────────────────────────────
echo -e "\n${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   RÉSUMÉ                                                     ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

echo -e "  ${GREEN}✓ PASS : $PASS${NC}"
echo -e "  ${RED}✗ FAIL : $FAIL${NC}"

if [ "$FAIL" -eq 0 ]; then
    echo -e "\n  ${GREEN}Tous les maillons sont opérationnels !${NC}"
    echo -e "  Tu peux maintenant allumer NAO et retester le maillon 6."
    exit 0
else
    echo -e "\n  ${RED}$FAIL maillon(s) en échec — voir les erreurs ci-dessus.${NC}"
    exit 1
fi
