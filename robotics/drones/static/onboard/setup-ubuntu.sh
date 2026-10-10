#!/usr/bin/env bash
# setup-ubuntu.sh — Installation du drone statique sur un PC (portable) Ubuntu
# avec webcam et micro. Automatise pc-portable-ubuntu.md, étapes 1 à 6, 9 et 10.
#
# Idempotent : on peut le relancer autant de fois qu'on veut ; il ne refait que
# ce qui manque et ne réécrit pas les réglages déjà faits dans .env.
#
# Usage (avec l'utilisateur du drone, pas en root) :
#   bash robotics/drones/static/onboard/setup-ubuntu.sh [options]
#
# Options :
#   --audio        Redemander le micro et le haut-parleur par défaut
#   --no-service   Ne pas installer le démarrage automatique (étape 9)
#   --no-power     Ne pas toucher à la mise en veille (étape 10)
#   -h, --help     Cette aide
#
# Sans terminal (ex. via ssh -T), les valeurs peuvent être passées par
# l'environnement : ROSBRIDGE_URL, ROSBRIDGE_TOKEN, CAMERA_INDEX, CAMERA_HFOV.
# Elles sont alors écrites dans .env.
#
# Le relais du NAO (ADR 006) s'installe ensuite avec nao-relay/setup-ubuntu.sh.

set -euo pipefail

ONBOARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=setup-lib.sh
. "${ONBOARD_DIR}/setup-lib.sh"

ENV_FILE="${ONBOARD_DIR}/.env"
VENV_DIR="${ONBOARD_DIR}/.venv"
UNIT_SRC="${ONBOARD_DIR}/static-drone-user.service"
UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_DST="${UNIT_DIR}/static-drone.service"
LOGIND_DROPIN="/etc/systemd/logind.conf.d/50-static-drone.conf"
SLEEP_TARGETS=(sleep.target suspend.target hibernate.target hybrid-sleep.target)

APT_PACKAGES=(git python3-venv libportaudio2 libusb-1.0-0 mpg123
              alsa-utils v4l-utils pulseaudio-utils)

OPT_AUDIO=0
OPT_SERVICE=1
OPT_POWER=1
for arg in "$@"; do
    case "$arg" in
        --audio)      OPT_AUDIO=1 ;;
        --no-service) OPT_SERVICE=0 ;;
        --no-power)   OPT_POWER=0 ;;
        -h|--help)    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            error "Option inconnue : $arg (voir --help)" ;;
    esac
done

# Ce qui a changé et demande de redémarrer le service à la fin
NEED_RESTART=0
ENV_CREATED=0

# ============================================================
# 1. Paquets
# ============================================================

install_packages() {
    section "Paquets"
    local missing=()
    for p in "${APT_PACKAGES[@]}"; do
        dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" \
            || missing+=("$p")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        log "Déjà installés : ${APT_PACKAGES[*]}"
        return
    fi
    info "Installation : ${missing[*]}"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    log "Paquets installés"
}

# ============================================================
# 2. Code : environnement virtuel Python
# ============================================================

setup_venv() {
    section "Environnement Python"
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        info "Création de ${VENV_DIR}"
        python3 -m venv "$VENV_DIR"
        NEED_RESTART=1
    else
        log "venv présent : ${VENV_DIR}"
    fi
    local before after
    before="$("${VENV_DIR}/bin/pip" freeze 2>/dev/null | md5sum)"
    "${VENV_DIR}/bin/pip" install -q --disable-pip-version-check -r "${ONBOARD_DIR}/requirements.txt"
    after="$("${VENV_DIR}/bin/pip" freeze 2>/dev/null | md5sum)"
    if [[ "$before" != "$after" ]]; then
        NEED_RESTART=1
        log "Dépendances installées"
    else
        log "Dépendances à jour"
    fi
}

# ============================================================
# 3. Droits : groupes audio et video
# ============================================================

setup_groups() {
    section "Groupes audio / video"
    local g missing=()
    for g in audio video; do
        id -nG "$USER" | tr ' ' '\n' | grep -qx "$g" || missing+=("$g")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        log "$USER est déjà dans audio et video"
        return
    fi
    sudo usermod -aG "$(IFS=,; echo "${missing[*]}")" "$USER"
    warn "$USER ajouté à ${missing[*]} : se déconnecter puis se reconnecter (ou redémarrer)"
}

# ============================================================
# 4. Micro et haut-parleur par défaut (PipeWire / PulseAudio)
# ============================================================

# choose_default sources|sinks : affiche la liste et propose d'en choisir un
choose_default() {
    local kind="$1" label="$2" current list choice name i=0
    if [[ "$kind" == sources ]]; then
        current="$(pactl get-default-source 2>/dev/null || true)"
        list="$(pactl list short sources | awk '$2 !~ /\.monitor$/ { print $2 }')"
    else
        current="$(pactl get-default-sink 2>/dev/null || true)"
        list="$(pactl list short sinks | awk '{ print $2 }')"
    fi
    [[ -n "$list" ]] || { warn "Aucun(e) ${label} trouvé(e)"; return; }
    echo "  ${label} disponibles :"
    while read -r name; do
        i=$((i + 1))
        if [[ "$name" == "$current" ]]; then
            echo "    $i) $name  (actuel)"
        else
            echo "    $i) $name"
        fi
    done <<< "$list"
    choice="$(ask "Numéro du ${label} (Entrée = garder l'actuel)" "")"
    [[ -n "$choice" ]] || { log "${label} : ${current:-défaut}"; return; }
    name="$(sed -n "${choice}p" <<< "$list")"
    [[ -n "$name" ]] || { warn "Numéro invalide, ${label} inchangé"; return; }
    if [[ "$kind" == sources ]]; then
        pactl set-default-source "$name"
    else
        pactl set-default-sink "$name"
    fi
    log "${label} : $name"
}

setup_audio() {
    section "Micro et haut-parleur"
    if ! pactl info >/dev/null 2>&1; then
        warn "Serveur de son injoignable (pas de session ?). Choisir plus tard, dans la session :"
        warn "  pactl set-default-source <micro> ; pactl set-default-sink <haut-parleur>"
        return
    fi
    if [[ $OPT_AUDIO -eq 0 && $ENV_CREATED -eq 0 ]] || ! is_interactive; then
        log "Micro : $(pactl get-default-source 2>/dev/null || echo '?')"
        log "Haut-parleur : $(pactl get-default-sink 2>/dev/null || echo '?')"
        info "Pour changer : relancer avec --audio, ou Paramètres > Son"
        return
    fi
    choose_default sources "micro"
    choose_default sinks "haut-parleur"
}

# ============================================================
# 5 et 6. Webcam et .env
# ============================================================

is_placeholder() {
    local v="$1"
    [[ -z "$v" || "$v" == "change-me" || "$v" == *example.com* ]]
}

setup_env() {
    section "Configuration (.env)"
    if [[ ! -f "$ENV_FILE" ]]; then
        cp "${ONBOARD_DIR}/.env.example" "$ENV_FILE"
        chmod 600 "$ENV_FILE"
        ENV_CREATED=1
        # Réglages d'un PC portable (pc-portable-ubuntu.md, étape 6)
        env_set "$ENV_FILE" RESPEAKER 0
        env_set "$ENV_FILE" MICS ""
        env_set "$ENV_FILE" CAMERA_HFOV 70
        log "Créé depuis .env.example : $ENV_FILE"
    else
        log "Existe déjà : $ENV_FILE (valeurs conservées)"
    fi
    local before
    before="$(md5sum < "$ENV_FILE")"

    # Valeurs passées par l'environnement : elles priment
    local key
    for key in ROSBRIDGE_URL ROSBRIDGE_TOKEN CAMERA_INDEX CAMERA_HFOV; do
        if [[ -n "${!key:-}" ]]; then
            env_set "$ENV_FILE" "$key" "${!key}"
            log "$key pris dans l'environnement"
        fi
    done

    # Connexion au VPS : demandée seulement tant qu'elle n'est pas renseignée
    local url domain token
    url="$(env_get "$ENV_FILE" ROSBRIDGE_URL)"
    domain="$(env_get "$ENV_FILE" DOMAIN)"
    if is_placeholder "$url" && is_placeholder "$domain"; then
        url="$(ask "URL rosbridge du VPS (ex. wss://rosbridge.mondomaine.fr)" "")"
        if [[ -n "$url" ]]; then
            env_set "$ENV_FILE" ROSBRIDGE_URL "$url"
        else
            warn "ROSBRIDGE_URL à renseigner dans $ENV_FILE"
        fi
    fi
    token="$(env_get "$ENV_FILE" ROSBRIDGE_TOKEN)"
    if is_placeholder "$token"; then
        token="$(ask_secret "ROSBRIDGE_TOKEN (même valeur que sur le VPS)")"
        if [[ -n "$token" ]]; then
            env_set "$ENV_FILE" ROSBRIDGE_TOKEN "$token"
        else
            warn "ROSBRIDGE_TOKEN à renseigner dans $ENV_FILE"
        fi
    fi

    # Webcam : demandée au premier passage seulement
    if [[ $ENV_CREATED -eq 1 && -z "${CAMERA_INDEX:-}" ]] && is_interactive; then
        echo "  Caméras (le premier /dev/video de chaque caméra donne l'image) :"
        v4l2-ctl --list-devices 2>/dev/null | sed 's/^/    /' || warn "Aucune caméra trouvée"
        local cam hfov
        cam="$(ask "CAMERA_INDEX : nom partiel (ex. C920), index ou chemin" "0")"
        env_set "$ENV_FILE" CAMERA_INDEX "$cam"
        hfov="$(ask "CAMERA_HFOV : champ de vision horizontal en degrés" "70")"
        env_set "$ENV_FILE" CAMERA_HFOV "$hfov"
    fi
    log "Caméra : CAMERA_INDEX=$(env_get "$ENV_FILE" CAMERA_INDEX), CAMERA_HFOV=$(env_get "$ENV_FILE" CAMERA_HFOV)"

    [[ "$(md5sum < "$ENV_FILE")" == "$before" ]] || NEED_RESTART=1
}

env_ready() {
    local url domain token
    url="$(env_get "$ENV_FILE" ROSBRIDGE_URL)"
    domain="$(env_get "$ENV_FILE" DOMAIN)"
    token="$(env_get "$ENV_FILE" ROSBRIDGE_TOKEN)"
    { ! is_placeholder "$url" || ! is_placeholder "$domain"; } && ! is_placeholder "$token"
}

# ============================================================
# 9. Démarrage automatique (service utilisateur)
# ============================================================

setup_service() {
    section "Démarrage automatique"
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        warn "systemd utilisateur injoignable : relancer depuis une session de $USER (bureau ou ssh)"
        return
    fi

    # Le fichier du dépôt suppose ~/ia-agent : on y met le vrai chemin
    local tmp
    tmp="$(mktemp)"
    sed "s|%h/ia-agent/robotics/drones/static/onboard|${ONBOARD_DIR}|g" "$UNIT_SRC" > "$tmp"
    mkdir -p "$UNIT_DIR"
    if install_if_changed "$tmp" "$UNIT_DST"; then
        systemctl --user daemon-reload
        NEED_RESTART=1
        log "Service installé : $UNIT_DST"
    else
        log "Service à jour : $UNIT_DST"
    fi
    rm -f "$tmp"

    if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
        sudo loginctl enable-linger "$USER"
        log "Linger activé : le service démarre au boot, sans session ouverte"
    else
        log "Linger déjà activé"
    fi

    systemctl --user enable -q static-drone
    if ! env_ready; then
        warn "Service activé mais pas lancé : compléter ROSBRIDGE_URL et ROSBRIDGE_TOKEN dans .env puis relancer ce script"
        return
    fi
    # Redémarre aussi après une modification de .env faite à la main
    if [[ $NEED_RESTART -eq 1 ]] || is_stale static-drone --user "$ENV_FILE" "$UNIT_DST"; then
        systemctl --user restart static-drone
        log "Service (re)démarré"
    else
        log "Service déjà lancé, rien de changé"
    fi
}

# ============================================================
# 10. Pas de mise en veille
# ============================================================

setup_power() {
    section "Mise en veille"
    local t to_mask=()
    for t in "${SLEEP_TARGETS[@]}"; do
        [[ "$(systemctl is-enabled "$t" 2>/dev/null || true)" == "masked" ]] || to_mask+=("$t")
    done
    if [[ ${#to_mask[@]} -gt 0 ]]; then
        sudo systemctl mask "${to_mask[@]}"
        log "Veille désactivée (${to_mask[*]})"
    else
        log "Veille déjà désactivée"
    fi

    # Capot fermé : ne rien faire. Fichier à part plutôt qu'une édition de
    # logind.conf : plus simple à relire et à annuler (supprimer le fichier).
    local tmp
    tmp="$(mktemp)"
    cat > "$tmp" <<'EOF'
# Drone statique (setup-ubuntu.sh) : le PC portable reste allumé capot fermé.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
    sudo mkdir -p "$(dirname "$LOGIND_DROPIN")"
    if install_if_changed "$tmp" "$LOGIND_DROPIN" sudo; then
        # Redémarrer systemd-logind fermerait la session graphique : on attend
        # le prochain redémarrage.
        warn "Capot fermé ignoré à partir du prochain redémarrage ($LOGIND_DROPIN)"
    else
        log "Capot fermé déjà ignoré"
    fi
    rm -f "$tmp"
    info "Garder le portable sur secteur. Capot fermé, utiliser une webcam et un micro USB."
}

# ============================================================
# MAIN
# ============================================================

main() {
    echo -e "${CYAN}Drone statique — installation Ubuntu${NC}  (${ONBOARD_DIR})"
    require_user_with_sudo
    require_ubuntu

    install_packages
    setup_venv
    setup_groups
    setup_env
    setup_audio
    if [[ $OPT_SERVICE -eq 1 ]]; then setup_service; fi
    if [[ $OPT_POWER -eq 1 ]]; then setup_power; fi

    section "Fini"
    if [[ $OPT_SERVICE -eq 1 ]]; then
        info "Journal : journalctl --user -u static-drone -f"
    else
        info "Lancer à la main : cd ${ONBOARD_DIR} && .venv/bin/python static_drone.py"
    fi
    info "Relais du NAO (ADR 006) : bash ${ONBOARD_DIR}/nao-relay/setup-ubuntu.sh"
}

main
