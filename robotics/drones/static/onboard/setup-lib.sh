# Fonctions communes à setup-ubuntu.sh et nao-relay/setup-ubuntu.sh.
# Ce fichier est sourcé, pas exécuté.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()    { echo -e "${GREEN}[✓]${NC} $*"; }
info()   { echo -e "${CYAN}[→]${NC} $*"; }
warn()   { echo -e "${YELLOW}[!]${NC} $*"; }
error()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
section(){ echo -e "\n${CYAN}══ $* ══${NC}"; }

# Le script tourne en utilisateur normal et appelle sudo quand il le faut :
# le venv, le .env et les services appartiennent à l'utilisateur du drone.
require_user_with_sudo() {
    [[ $EUID -ne 0 ]] || error "Lancer sans sudo, avec l'utilisateur du drone : bash $0"
    command -v sudo >/dev/null || error "sudo est nécessaire"
    sudo -v || error "sudo refusé"
}

require_ubuntu() {
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        if [[ "${ID:-}" != "ubuntu" ]]; then
            warn "Système ${PRETTY_NAME:-inconnu} : script prévu pour Ubuntu 22.04 / 24.04"
        fi
    fi
}

is_interactive() { [[ -t 0 ]]; }

# ask "Question" [défaut] → réponse sur stdout (défaut si pas de terminal)
ask() {
    local prompt="$1" default="${2:-}" value=""
    if is_interactive; then
        if [[ -n "$default" ]]; then
            read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} [${default}] : ")" value
        else
            read -rp "$(echo -e "${YELLOW}?${NC} ${prompt} : ")" value
        fi
    fi
    echo "${value:-$default}"
}

ask_secret() {
    local prompt="$1" value=""
    if is_interactive; then
        read -rsp "$(echo -e "${YELLOW}?${NC} ${prompt} : ")" value
        echo >&2
    fi
    echo "$value"
}

# ── Fichier .env (KEY=valeur, une par ligne) ────────────────────────────────
# Le .env n'est jamais sourcé : certaines valeurs (PLAYER_CMD=mpg123 -q -) ne
# sont pas du bash valide.

# env_get FICHIER CLÉ → dernière valeur non commentée (vide si absente)
env_get() {
    local file="$1" key="$2"
    [[ -f "$file" ]] || return 0
    awk -v k="$key" '
        index($0, k "=") == 1 { v = substr($0, length(k) + 2); found = 1 }
        END { if (found) print v }
    ' "$file"
}

# env_has FICHIER CLÉ → vrai si une ligne non commentée CLÉ= existe
env_has() {
    [[ -f "$1" ]] && grep -q "^$2=" "$1"
}

# env_set FICHIER CLÉ VALEUR : remplace la ligne CLÉ=, sinon décommente la
# première ligne #CLÉ=, sinon ajoute à la fin. Ne touche pas au reste.
env_set() {
    local file="$1" key="$2" value="$3" tmp
    tmp="$(mktemp)"
    KEY="$key" VALUE="$value" awk '
        BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"] }
        {
            line[NR] = $0
            if (!exact && index($0, k "=") == 1) exact = NR
            if (!comm && index($0, "#" k "=") == 1) comm = NR
        }
        END {
            target = exact ? exact : comm
            for (i = 1; i <= NR; i++) {
                if (i == target) print k "=" v
                else if (index(line[i], k "=") == 1) continue   # doublons
                else print line[i]
            }
            if (!target) print k "=" v
        }
    ' "$file" > "$tmp"
    cat "$tmp" > "$file"
    rm -f "$tmp"
}

# install_if_changed SOURCE DESTINATION [sudo] → vrai (0) si le fichier a changé
install_if_changed() {
    local src="$1" dst="$2" use_sudo="${3:-}"
    if [[ -f "$dst" ]] && cmp -s "$src" "$dst"; then
        return 1
    fi
    if [[ -n "$use_sudo" ]]; then
        sudo install -m 644 "$src" "$dst"
    else
        install -m 644 "$src" "$dst"
    fi
    return 0
}

# is_stale SERVICE [--user] FICHIER… → vrai si le service ne tourne pas, ou si
# l'un des fichiers a été modifié après son démarrage (réglage à appliquer).
is_stale() {
    local unit="$1"; shift
    local sc=(systemctl)
    if [[ "${1:-}" == "--user" ]]; then sc=(systemctl --user); shift; fi
    "${sc[@]}" is-active -q "$unit" || return 0
    local started f
    started="$(date -d "$("${sc[@]}" show "$unit" -p ActiveEnterTimestamp --value)" +%s 2>/dev/null || echo 0)"
    for f in "$@"; do
        [[ -e "$f" && "$(stat -c %Y "$f")" -gt "$started" ]] && return 0
    done
    return 1
}
