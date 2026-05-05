#!/usr/bin/env bash
# run-tests.sh — Lanceur de tests pour la chaîne WebUI → NAO
#
# Usage :
#   bash tests/run-tests.sh                          # liste les suites disponibles
#   bash tests/run-tests.sh <suite> [<suite> ...]    # lance les suites indiquées
#   bash tests/run-tests.sh all                      # lance toutes les suites
#
# Suites disponibles :
#   chain        test-chain.sh          — maillons WebUI → Ollama → naoqi-bridge
#   capabilities test-nao-capabilities.sh — capacités NAOqi (mock par défaut)
#   capabilities:nao                   — capacités NAOqi avec NAO physique (--nao)
#   diag         diag-nao-connection.sh — diagnostic de connexion NAO
#
# Exemples :
#   bash tests/run-tests.sh chain
#   bash tests/run-tests.sh capabilities
#   bash tests/run-tests.sh capabilities:nao
#   bash tests/run-tests.sh chain capabilities
#   bash tests/run-tests.sh all

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# ─── Aide / liste des suites ──────────────────────────────────────────────────
_usage() {
    echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${YELLOW}║   run-tests.sh — Lanceur de suites de tests                 ║${NC}"
    echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${CYAN}Usage :${NC}"
    echo -e "    bash tests/run-tests.sh <suite> [<suite> ...]"
    echo -e "    bash tests/run-tests.sh all"
    echo ""
    echo -e "  ${CYAN}Suites disponibles :${NC}"
    echo ""
    echo -e "    ${GREEN}chain${NC}"
    echo -e "        Teste chaque maillon de la chaîne WebUI → Ollama → naoqi-bridge"
    echo -e "        (NAO peut être éteint — mock mode automatique)"
    echo ""
    echo -e "    ${GREEN}capabilities${NC}"
    echo -e "        Teste toutes les capacités NAOqi via naoqi-bridge (mock mode)"
    echo -e "        /health, /battery, /say, /leds, /posture, /motion, /move, /events"
    echo ""
    echo -e "    ${GREEN}capabilities:nao${NC}"
    echo -e "        Idem capabilities mais avec NAO physique allumé (--nao)"
    echo -e "        Requiert NAO connecté et naoqi-bridge en mode réel"
    echo ""
    echo -e "    ${GREEN}diag${NC}"
    echo -e "        Diagnostic de connexion NAO (réseau, qi SDK, port 9559)"
    echo ""
    echo -e "    ${GREEN}all${NC}"
    echo -e "        Lance : chain + capabilities + diag"
    echo -e "        (pas capabilities:nao — requiert NAO allumé)"
    echo ""
    echo -e "  ${CYAN}Exemples :${NC}"
    echo -e "    bash tests/run-tests.sh chain"
    echo -e "    bash tests/run-tests.sh capabilities"
    echo -e "    bash tests/run-tests.sh capabilities:nao"
    echo -e "    bash tests/run-tests.sh chain capabilities"
    echo -e "    bash tests/run-tests.sh all"
    echo ""
}

# ─── Exécution d'une suite ────────────────────────────────────────────────────
TOTAL_PASS=0
TOTAL_FAIL=0
SUITES_RUN=()
SUITES_FAILED=()

_run_suite() {
    local suite="$1"
    local script args label

    case "$suite" in
        chain)
            script="$SCRIPT_DIR/test-chain.sh"
            args=()
            label="chain — maillons WebUI → NAO"
            ;;
        capabilities)
            script="$SCRIPT_DIR/test-nao-capabilities.sh"
            args=()
            label="capabilities — capacités NAOqi (mock)"
            ;;
        capabilities:nao)
            script="$SCRIPT_DIR/test-nao-capabilities.sh"
            args=(--nao)
            label="capabilities:nao — capacités NAOqi (NAO physique)"
            ;;
        diag)
            script="$SCRIPT_DIR/diag-nao-connection.sh"
            args=()
            label="diag — diagnostic connexion NAO"
            ;;
        *)
            echo -e "${RED}Suite inconnue : '$suite'${NC}" >&2
            echo -e "Lancez ${CYAN}bash tests/run-tests.sh${NC} sans argument pour voir les suites disponibles." >&2
            SUITES_RUN+=("${RED}✗${NC} $suite (inconnue)")
            SUITES_FAILED+=("$suite")
            return 1
            ;;
    esac

    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${YELLOW}  ▶  $label${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    if bash "$script" "${args[@]}"; then
        SUITES_RUN+=("${GREEN}✓${NC} $suite")
    else
        SUITES_RUN+=("${RED}✗${NC} $suite")
        SUITES_FAILED+=("$suite")
    fi
}

# ─── Point d'entrée ───────────────────────────────────────────────────────────
if [ $# -eq 0 ]; then
    _usage
    exit 0
fi

# Résolution de "all"
REQUESTED=()
for arg in "$@"; do
    if [ "$arg" = "all" ]; then
        REQUESTED+=(chain capabilities diag)
    else
        REQUESTED+=("$arg")
    fi
done

# Dédoublonnage (ordre conservé)
SEEN=()
DEDUPED=()
for s in "${REQUESTED[@]}"; do
    already=0
    for seen in "${SEEN[@]:-}"; do
        [ "$seen" = "$s" ] && already=1 && break
    done
    if [ "$already" -eq 0 ]; then
        DEDUPED+=("$s")
        SEEN+=("$s")
    fi
done

# Lancement des suites
for suite in "${DEDUPED[@]}"; do
    _run_suite "$suite"
done

# ─── Résumé global ────────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║   RÉSUMÉ GLOBAL                                              ║${NC}"
echo -e "${YELLOW}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
for line in "${SUITES_RUN[@]}"; do
    echo -e "    $line"
done
echo ""

if [ ${#SUITES_FAILED[@]} -eq 0 ]; then
    echo -e "  ${GREEN}Toutes les suites ont réussi.${NC}"
    exit 0
else
    echo -e "  ${RED}${#SUITES_FAILED[@]} suite(s) en échec : ${SUITES_FAILED[*]}${NC}"
    exit 1
fi
