#!/usr/bin/env bash
# test-nao-capabilities.sh — Teste les capacités NAOqi via naoqi-bridge
#
# Usage :
#   bash tests/test-nao-capabilities.sh [--nao] [<section> ...]
#
# Sans section : lance toutes les sections.
# Avec sections : lance uniquement celles indiquées.
#
# Options :
#   --nao     Active les tests physiques (NAO doit être allumé et connecté)
#             Par défaut : mock mode uniquement
#
# Sections :
#   health        /health — état de connexion
#   battery       /battery — niveau de batterie
#   say           /say — synthèse vocale (ALTextToSpeech)
#   leds          /leds — contrôle LEDs (ALLeds.fadeRGB)
#   posture       /posture — postures prédéfinies (ALRobotPosture)
#   motion        /motion — articulations tête (ALMotion)
#   move          /move + /move/stop — déplacement (ALMotion.moveToward)
#   events        /events — flux SSE (bumper, sonar, batterie)
#   robustness    cas d'erreur et endpoints invalides

set -uo pipefail

# ─── Parsing des arguments ────────────────────────────────────────────────────
PHYSICAL=0
SECTIONS=()

for arg in "$@"; do
    case "$arg" in
        --nao) PHYSICAL=1 ;;
        health|battery|say|leds|posture|motion|move|events|robustness)
            SECTIONS+=("$arg") ;;
        *)
            echo "Argument inconnu : '$arg'" >&2
            echo "Sections valides : health battery say leds posture motion move events robustness" >&2
            exit 1
            ;;
    esac
done

# Par défaut : toutes les sections
if [ ${#SECTIONS[@]} -eq 0 ]; then
    SECTIONS=(health battery say leds posture motion move events robustness)
fi

# ─── Configuration ────────────────────────────────────────────────────────────
BRIDGE="http://localhost:8090"
TIMEOUT=10

# ─── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

PASS=0
FAIL=0
SKIP=0

_pass()  { echo -e "  ${GREEN}✓ PASS${NC}  $1"; PASS=$((PASS + 1)); }
_fail()  { echo -e "  ${RED}✗ FAIL${NC}  $1"; FAIL=$((FAIL + 1)); }
_skip()  { echo -e "  ${BLUE}⊘ SKIP${NC}  $1"; SKIP=$((SKIP + 1)); }
_info()  { echo -e "        ${CYAN}$1${NC}"; }
_head()  { echo -e "\n${YELLOW}┌─ $1 ─────────────────────────────────────${NC}"; }
_sub()   { echo -e "  ${MAGENTA}▸${NC} $1"; }
_warn()  { echo -e "  ${YELLOW}⚠${NC}  $1"; }

_post() {
    local desc="$1" body="$2" endpoint="$3" out
    out=$(curl -s --max-time "$TIMEOUT" -X POST "${BRIDGE}${endpoint}" \
          -H "Content-Type: application/json" -d "$body" 2>&1)
    if echo "$out" | grep -q '"ok".*true'; then
        _pass "$desc"; _info "$out"
    else
        _fail "$desc"; [ -n "$out" ] && _info "$(echo "$out" | head -5)"
    fi
}

_get() {
    local desc="$1" endpoint="$2" pattern="${3:-}" out
    out=$(curl -s --max-time "$TIMEOUT" "${BRIDGE}${endpoint}" 2>&1)
    if [ -z "$pattern" ] || echo "$out" | grep -qE "$pattern"; then
        _pass "$desc"; _info "$out"
    else
        _fail "$desc (pattern '$pattern' absent)"; _info "$out"
    fi
}

# ─── Sections ─────────────────────────────────────────────────────────────────

_section_health() {
    _head "0 — Prérequis / health"

    _get "naoqi-bridge /health répond" "/health" '"ok"'

    if [ "$PHYSICAL" -eq 1 ]; then
        HEALTH=$(curl -sf --max-time 5 "${BRIDGE}/health" 2>/dev/null || echo "{}")
        CONNECTED=$(echo "$HEALTH" | python3 -c \
            "import json,sys; d=json.load(sys.stdin); print('true' if d.get('connected') else 'false')" \
            2>/dev/null || echo "false")
        if [ "$CONNECTED" = "true" ]; then
            _info "NAO connecté — mode NAO physique actif"
        else
            _fail "connected=false — NAO non connecté, impossible de lancer --nao"
            _info "$HEALTH"
            exit 1
        fi
    else
        _info "Mode mock — commandes simulées (relancer avec --nao quand NAO est allumé)"
    fi
}

_section_battery() {
    _head "1 — Batterie  (GET /battery)"
    _sub "Lecture du niveau de charge"
    _get "GET /battery → niveau retourné" "/battery" '"level"'

    if [ "$PHYSICAL" -eq 1 ]; then
        LEVEL=$(curl -sf --max-time 5 "${BRIDGE}/battery" 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin).get('level','?'))" 2>/dev/null)
        _info "Niveau batterie : ${LEVEL}%"
    fi
}

_section_say() {
    _head "2 — Synthèse vocale  (POST /say)"

    _sub "Texte vide → erreur 400"
    out=$(curl -s --max-time 5 -X POST "${BRIDGE}/say" \
          -H "Content-Type: application/json" -d '{"text":""}' 2>&1)
    if echo "$out" | grep -q '"error"'; then
        _pass "/say texte vide → {error} attendu"; _info "$out"
    else
        _fail "/say texte vide devrait retourner {error}"; _info "$out"
    fi

    _sub "Phrase courte"
    _post "/say 'Bonjour'" '{"text":"Bonjour"}' "/say"

    _sub "Phrase avec accents (UTF-8)"
    _post "/say UTF-8 — accents" '{"text":"Je suis prêt à t'\''aider"}' "/say"

    _sub "Phrase longue (> 50 car.)"
    _post "/say phrase longue" \
        '{"text":"Je suis le robot NAO. Je peux parler, bouger et interagir avec vous."}' "/say"

    if [ "$PHYSICAL" -eq 1 ]; then
        _sub "Chiffres et ponctuation"
        _post "/say chiffres + ponctuation" \
            '{"text":"Deux plus deux font quatre. Est-ce correct ?"}' "/say"

        _sub "Question (intonation)"
        _post "/say question" '{"text":"Comment allez-vous ?"}' "/say"
    fi
}

_section_leds() {
    _head "3 — LEDs  (POST /leds)"

    _sub "AllLeds rouge"
    _post "AllLeds → rouge" \
        '{"group":"AllLeds","r":1.0,"g":0.0,"b":0.0,"duration":0.5}' "/leds"

    _sub "FaceLeds vert"
    _post "FaceLeds → vert" \
        '{"group":"FaceLeds","r":0.0,"g":1.0,"b":0.0,"duration":0.5}' "/leds"

    _sub "EarLeds bleu"
    _post "EarLeds → bleu" \
        '{"group":"EarLeds","r":0.0,"g":0.0,"b":1.0,"duration":0.5}' "/leds"

    _sub "ChestLeds blanc"
    _post "ChestLeds → blanc" \
        '{"group":"ChestLeds","r":1.0,"g":1.0,"b":1.0,"duration":0.5}' "/leds"

    _sub "AllLeds extinction (noir)"
    _post "AllLeds → extinction" \
        '{"group":"AllLeds","r":0.0,"g":0.0,"b":0.0,"duration":1.0}' "/leds"

    _sub "Durée courte (flash 0.1s)"
    _post "AllLeds flash rapide" \
        '{"group":"AllLeds","r":1.0,"g":1.0,"b":0.0,"duration":0.1}' "/leds"

    _sub "Durée longue (fondu 3s)"
    _post "FaceLeds fondu lent 3s" \
        '{"group":"FaceLeds","r":0.5,"g":0.0,"b":0.5,"duration":3.0}' "/leds"

    _sub "Valeurs RGB à la limite (1.0, 1.0, 1.0)"
    _post "AllLeds valeurs max" \
        '{"group":"AllLeds","r":1.0,"g":1.0,"b":1.0,"duration":0.5}' "/leds"

    _sub "Valeurs RGB à 0 (extinction totale)"
    _post "AllLeds valeurs min" \
        '{"group":"AllLeds","r":0.0,"g":0.0,"b":0.0,"duration":0.5}' "/leds"
}

_section_posture() {
    _head "4 — Postures  (POST /posture)"

    _sub "Posture Stand (debout)"
    _post "posture Stand" '{"posture":"Stand","speed":0.5}' "/posture"

    _sub "Posture Crouch (accroupi)"
    _post "posture Crouch" '{"posture":"Crouch","speed":0.5}' "/posture"

    _sub "Posture StandInit (init standard)"
    _post "posture StandInit" '{"posture":"StandInit","speed":0.5}' "/posture"

    if [ "$PHYSICAL" -eq 1 ]; then
        _sub "Posture Sit (assis)"
        _post "posture Sit" '{"posture":"Sit","speed":0.3}' "/posture"

        _sub "Posture SitRelax (assis relâché)"
        _post "posture SitRelax" '{"posture":"SitRelax","speed":0.3}' "/posture"

        _sub "Posture LyingBelly (allongé ventre)"
        _post "posture LyingBelly (lent)" '{"posture":"LyingBelly","speed":0.2}' "/posture"

        _sub "Posture LyingBack (allongé dos)"
        _post "posture LyingBack (lent)" '{"posture":"LyingBack","speed":0.2}' "/posture"

        _sub "Retour Stand vitesse rapide (0.8)"
        _post "posture Stand rapide" '{"posture":"Stand","speed":0.8}' "/posture"

        _sub "Retour Stand vitesse lente (0.1)"
        _post "posture Stand lent" '{"posture":"Stand","speed":0.1}' "/posture"
    else
        _skip "postures Sit/Lying ignorées en mode mock (--nao pour tester)"
    fi
}

_section_motion() {
    _head "5 — Articulations tête  (POST /motion)"
    # HeadYaw [-2.09, 2.09] rad  |  HeadPitch [-0.67, 0.51] rad

    _sub "HeadYaw → 0 (centre)"
    _post "HeadYaw centre (0 rad)" \
        '{"joint":"HeadYaw","angle":0.0,"speed":0.2}' "/motion"

    _sub "HeadYaw → gauche (+0.5 rad ≈ 29°)"
    _post "HeadYaw gauche (+0.5 rad)" \
        '{"joint":"HeadYaw","angle":0.5,"speed":0.2}' "/motion"

    _sub "HeadYaw → droite (-0.5 rad ≈ -29°)"
    _post "HeadYaw droite (-0.5 rad)" \
        '{"joint":"HeadYaw","angle":-0.5,"speed":0.2}' "/motion"

    _sub "HeadYaw → max gauche (+2.0 rad ≈ 115°)"
    _post "HeadYaw max gauche (+2.0 rad)" \
        '{"joint":"HeadYaw","angle":2.0,"speed":0.15}' "/motion"

    _sub "HeadYaw → max droite (-2.0 rad ≈ -115°)"
    _post "HeadYaw max droite (-2.0 rad)" \
        '{"joint":"HeadYaw","angle":-2.0,"speed":0.15}' "/motion"

    _sub "HeadPitch → 0 (horizontal)"
    _post "HeadPitch horizontal (0 rad)" \
        '{"joint":"HeadPitch","angle":0.0,"speed":0.2}' "/motion"

    _sub "HeadPitch → bas (+0.4 rad ≈ 23°)"
    _post "HeadPitch bas (+0.4 rad)" \
        '{"joint":"HeadPitch","angle":0.4,"speed":0.2}' "/motion"

    _sub "HeadPitch → haut (-0.5 rad ≈ -29°)"
    _post "HeadPitch haut (-0.5 rad)" \
        '{"joint":"HeadPitch","angle":-0.5,"speed":0.2}' "/motion"

    _sub "Vitesse interpolation lente (0.05)"
    _post "HeadYaw vitesse 0.05 (lent)" \
        '{"joint":"HeadYaw","angle":0.0,"speed":0.05}' "/motion"

    _sub "Vitesse interpolation rapide (0.5)"
    _post "HeadYaw vitesse 0.5 (rapide)" \
        '{"joint":"HeadYaw","angle":0.0,"speed":0.5}' "/motion"
}

_section_move() {
    _head "6 — Déplacement  (POST /move  +  POST /move/stop)"

    _sub "Arrêt immédiat (sans mouvement préalable)"
    _post "/move/stop sans contexte" '{}' "/move/stop"

    _sub "Avancer (x=0.3 m/s)"
    _post "/move avancer x=0.3" '{"x":0.3,"y":0.0,"theta":0.0}' "/move"

    _sub "Arrêt après avancer"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Reculer (x=-0.3 m/s)"
    _post "/move reculer x=-0.3" '{"x":-0.3,"y":0.0,"theta":0.0}' "/move"

    _sub "Arrêt"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Strafe gauche (y=0.2 m/s)"
    _post "/move strafe gauche y=0.2" '{"x":0.0,"y":0.2,"theta":0.0}' "/move"

    _sub "Arrêt"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Strafe droite (y=-0.2 m/s)"
    _post "/move strafe droite y=-0.2" '{"x":0.0,"y":-0.2,"theta":0.0}' "/move"

    _sub "Arrêt"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Rotation gauche (theta=0.5 rad/s)"
    _post "/move rotation gauche theta=0.5" '{"x":0.0,"y":0.0,"theta":0.5}' "/move"

    _sub "Arrêt"
    _post "/move/stop" '{}' "/move/stop"

    _sub "Rotation droite (theta=-0.5 rad/s)"
    _post "/move rotation droite theta=-0.5" '{"x":0.0,"y":0.0,"theta":-0.5}' "/move"

    _sub "Arrêt final"
    _post "/move/stop final" '{}' "/move/stop"

    if [ "$PHYSICAL" -eq 1 ]; then
        _sub "Déplacement combiné (avance + rotation)"
        _post "/move combiné x=0.2 theta=0.3" '{"x":0.2,"y":0.0,"theta":0.3}' "/move"
        sleep 2
        _post "/move/stop après déplacement combiné" '{}' "/move/stop"
    else
        _skip "déplacement combiné ignoré en mode mock (--nao pour tester)"
    fi
}

_section_events() {
    _head "7 — Flux SSE  (GET /events)"

    _sub "Connexion SSE et réception du keepalive"
    out=$(curl -s --max-time 20 -N -H "Accept: text/event-stream" \
          "${BRIDGE}/events" 2>&1 | head -5 || true)
    if echo "$out" | grep -qE "data:|keepalive"; then
        _pass "/events stream SSE actif (keepalive ou data reçu)"; _info "$out"
    else
        _fail "/events stream SSE : aucune donnée reçue en 20 s"
        [ -n "$out" ] && _info "$out"
    fi

    if [ "$PHYSICAL" -eq 1 ]; then
        _sub "Batterie disponible dans le stream (poll 30 s — attend 35 s)"
        _warn "Ce test attend jusqu'à 35 s (polling batterie)"
        out=$(curl -s --max-time 40 -N -H "Accept: text/event-stream" \
              "${BRIDGE}/events" 2>&1 \
              | grep -m1 '"topic":"battery"' || true)
        if [ -n "$out" ]; then
            _pass "/events topic battery reçu"; _info "$out"
        else
            _fail "/events topic battery absent en 40 s"
        fi
    else
        _skip "test batterie SSE ignoré (mode mock)"
    fi
}

_section_robustness() {
    _head "8 — Robustesse / cas d'erreur"

    _sub "/say sans body JSON"
    code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
           -X POST "${BRIDGE}/say" 2>&1)
    if echo "$code" | grep -qE "^[45]"; then
        _pass "/say sans body → HTTP $code (erreur attendue)"
    else
        _fail "/say sans body devrait retourner 4xx ou 5xx (obtenu: $code)"
    fi

    _sub "/motion avec joint invalide"
    out=$(curl -s --max-time 10 -X POST "${BRIDGE}/motion" \
          -H "Content-Type: application/json" \
          -d '{"joint":"JointInexistant","angle":0.5,"speed":0.1}' 2>&1)
    if [ "$PHYSICAL" -eq 1 ]; then
        if echo "$out" | grep -q '"error"'; then
            _pass "/motion joint invalide → erreur NAOqi retournée (correct)"
        else
            _fail "/motion joint invalide → NAOqi aurait dû rejeter ce joint"
        fi
    else
        if echo "$out" | grep -q '"ok".*true'; then
            _pass "/motion joint invalide → mock retourne ok:true (normal)"
        else
            _fail "/motion joint invalide → réponse inattendue du mock"
        fi
    fi
    _info "$out"

    _sub "/posture avec nom invalide"
    out=$(curl -s --max-time 10 -X POST "${BRIDGE}/posture" \
          -H "Content-Type: application/json" \
          -d '{"posture":"PostureInexistante","speed":0.5}' 2>&1)
    if [ "$PHYSICAL" -eq 1 ]; then
        if echo "$out" | grep -q '"error"'; then
            _pass "/posture invalide → erreur NAOqi retournée (correct)"
        else
            _fail "/posture invalide → NAOqi aurait dû rejeter cette posture"
        fi
    else
        if echo "$out" | grep -q '"ok".*true'; then
            _pass "/posture invalide → mock retourne ok:true (normal)"
        else
            _fail "/posture invalide → réponse inattendue du mock"
        fi
    fi
    _info "$out"

    _sub "Endpoint inexistant → 404"
    code=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" \
           "${BRIDGE}/inexistant" 2>&1)
    if [ "$code" = "404" ]; then
        _pass "GET /inexistant → 404"
    else
        _fail "GET /inexistant devrait retourner 404 (obtenu: $code)"
    fi
}

# ─── En-tête ──────────────────────────────────────────────────────────────────
echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   test-nao-capabilities.sh — Capacités NAOqi via bridge     ║${NC}"
if [ "$PHYSICAL" -eq 1 ]; then
echo -e "${YELLOW}║   Mode : NAO physique  (--nao)                               ║${NC}"
else
echo -e "${YELLOW}║   Mode : mock  (relancer avec --nao quand NAO est allumé)    ║${NC}"
fi
echo -e "${YELLOW}║   Sections : ${SECTIONS[*]}$(printf '%*s' $((44 - ${#SECTIONS[*]} * 1)) '')║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

# ─── Dispatch des sections ────────────────────────────────────────────────────
for section in "${SECTIONS[@]}"; do
    case "$section" in
        health)      _section_health ;;
        battery)     _section_battery ;;
        say)         _section_say ;;
        leds)        _section_leds ;;
        posture)     _section_posture ;;
        motion)      _section_motion ;;
        move)        _section_move ;;
        events)      _section_events ;;
        robustness)  _section_robustness ;;
    esac
done

# ─── Résumé ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   RÉSUMÉ                                                     ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"

echo -e "  ${GREEN}✓ PASS : $PASS${NC}"
echo -e "  ${RED}✗ FAIL : $FAIL${NC}"
echo -e "  ${BLUE}⊘ SKIP : $SKIP${NC}"

if [ "$PHYSICAL" -eq 0 ] && [ ${#SECTIONS[@]} -ge 5 ]; then
    echo ""
    echo -e "  ${CYAN}Mode mock — relancer avec --nao quand NAO est allumé${NC}"
fi

echo ""
if [ "$FAIL" -eq 0 ]; then
    echo -e "  ${GREEN}Toutes les capacités NAOqi répondent correctement.${NC}"
    exit 0
else
    echo -e "  ${RED}$FAIL capacité(s) en échec — voir les erreurs ci-dessus.${NC}"
    exit 1
fi
