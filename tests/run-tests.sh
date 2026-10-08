#!/usr/bin/env bash
# run-tests.sh — Lanceur de tests pour la chaîne WebUI → NAO
#
# Usage :
#   bash tests/run-tests.sh                          # liste les suites disponibles
#   bash tests/run-tests.sh <suite> [<suite> ...]    # lance les suites indiquées
#   bash tests/run-tests.sh all                      # lance toutes les suites (sans --nao)
#
# Exemples :
#   bash tests/run-tests.sh chain
#   bash tests/run-tests.sh nao:say nao:leds
#   bash tests/run-tests.sh nao:move --nao
#   bash tests/run-tests.sh nao:posture nao:motion nao:move --nao
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
    echo -e "    bash tests/run-tests.sh <suite> [<suite> ...] [--nao]"
    echo -e "    bash tests/run-tests.sh all"
    echo ""
    echo -e "  ${CYAN}Options :${NC}"
    echo -e "    ${GREEN}--nao${NC}   Active les tests NAO physique pour les suites nao:*"
    echo ""
    echo -e "  ${CYAN}Suites — infrastructure :${NC}"
    echo ""
    echo -e "    ${GREEN}chain${NC}"
    echo -e "        Maillons WebUI → Ollama → naoqi-bridge (NAO peut être éteint)"
    echo -e "        Inclut le maillon 8 : pipe __ping__ via l'API Open WebUI"
    echo -e "        (nécessite WEBUI_API_KEY défini dans l'environnement)"
    echo ""
    echo -e "    ${GREEN}diag${NC}"
    echo -e "        Diagnostic de connexion NAO (réseau, qi SDK, port 9559)"
    echo ""
    echo -e "  ${CYAN}Suites — capacités NAOqi (mock par défaut, --nao pour physique) :${NC}"
    echo ""
    echo -e "    ${GREEN}nao:health${NC}       /health — état de connexion bridge"
    echo -e "    ${GREEN}nao:battery${NC}      /battery — niveau de batterie"
    echo -e "    ${GREEN}nao:say${NC}          /say — synthèse vocale (ALTextToSpeech)"
    echo -e "    ${GREEN}nao:leds${NC}         /leds — contrôle LEDs (ALLeds.fadeRGB)"
    echo -e "    ${GREEN}nao:posture${NC}      /posture — postures (Stand, Crouch, Sit…)"
    echo -e "    ${GREEN}nao:motion${NC}       /motion — articulations tête (HeadYaw, HeadPitch)"
    echo -e "    ${GREEN}nao:move${NC}         /move + /move/stop — déplacement locomotion"
    echo -e "    ${GREEN}nao:events${NC}       /events — flux SSE (bumper, sonar, batterie)"
    echo -e "    ${GREEN}nao:robustness${NC}   cas d'erreur et endpoints invalides"
    echo -e "    ${GREEN}nao:speaking${NC}     animation « je parle » (yeux, tête, bras) du NAO émotif"
    echo -e "    ${GREEN}nao:all${NC}          toutes les sections nao:* ci-dessus"
    echo ""
    echo -e "  ${CYAN}Raccourcis :${NC}"
    echo ""
    echo -e "    ${GREEN}all${NC}   chain + nao:all + diag  (sans --nao)"
    echo ""
    echo -e "  ${CYAN}Exemples :${NC}"
    echo -e "    bash tests/run-tests.sh chain"
    echo -e "    bash tests/run-tests.sh nao:say nao:leds"
    echo -e "    bash tests/run-tests.sh nao:move --nao"
    echo -e "    bash tests/run-tests.sh nao:posture nao:motion nao:move --nao"
    echo -e "    bash tests/run-tests.sh all"
    echo ""
}

# ─── Gestion des suites ───────────────────────────────────────────────────────
SUITES_RUN=()
SUITES_FAILED=()
PHYSICAL=0

# Sections NAO individuelles disponibles
NAO_SECTIONS=(health battery say leds posture motion move events robustness)

_run_suite() {
    local suite="$1"
    local script args label nao_section

    case "$suite" in
        chain)
            script="$SCRIPT_DIR/test-chain.sh"
            args=()
            label="chain — maillons WebUI → NAO"
            ;;
        diag)
            script="$SCRIPT_DIR/diag-nao-connection.sh"
            args=()
            label="diag — diagnostic connexion NAO"
            ;;
        nao:all)
            # Expansé en amont — ne devrait pas arriver ici
            for s in "${NAO_SECTIONS[@]}"; do _run_suite "nao:$s"; done
            return
            ;;
        nao:health|nao:battery|nao:say|nao:leds|nao:posture|nao:motion|nao:move|nao:events|nao:robustness|nao:speaking)
            nao_section="${suite#nao:}"
            script="$SCRIPT_DIR/test-nao-capabilities.sh"
            args=("$nao_section")
            [ "$PHYSICAL" -eq 1 ] && args=(--nao "$nao_section")
            label="$suite$([ "$PHYSICAL" -eq 1 ] && echo ' (--nao)' || echo ' (mock)')"
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

# Parsing : sépare --nao des noms de suites
RAW_SUITES=()
for arg in "$@"; do
    case "$arg" in
        --nao) PHYSICAL=1 ;;
        *)     RAW_SUITES+=("$arg") ;;
    esac
done

# Expansion de "all" et "nao:all"
EXPANDED=()
for arg in "${RAW_SUITES[@]}"; do
    case "$arg" in
        all)
            EXPANDED+=(chain)
            for s in "${NAO_SECTIONS[@]}"; do EXPANDED+=("nao:$s"); done
            EXPANDED+=(diag)
            ;;
        nao:all)
            for s in "${NAO_SECTIONS[@]}"; do EXPANDED+=("nao:$s"); done
            ;;
        *)
            EXPANDED+=("$arg")
            ;;
    esac
done

# Dédoublonnage (ordre conservé)
SEEN=()
DEDUPED=()
for s in "${EXPANDED[@]}"; do
    already=0
    for seen in "${SEEN[@]:-}"; do [ "$seen" = "$s" ] && already=1 && break; done
    if [ "$already" -eq 0 ]; then
        DEDUPED+=("$s"); SEEN+=("$s")
    fi
done

# Lancement
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
