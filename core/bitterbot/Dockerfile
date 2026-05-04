# Dockerfile maintenu par monolithoid/ia-agent
# Remplace le Dockerfile upstream de Bitterbot-AI/bitterbot-desktop
# qui référence des chemins absents du repo (ui/, patches/, pnpm ui:build).
#
# Différences avec l'upstream :
#   + RUN apt-get : dépendances système pour les addons natifs
#                   (absentes de node:22-bookworm par défaut)
#   - COPY ui/package.json  (ui/ n'existe pas dans le repo)
#   - COPY patches          (pas de patchedDependencies → mkdir vide suffit)
#   - RUN pnpm ui:build     (script absent du package.json)

FROM node:22-slim

# Install Bun (required for build scripts)
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"

RUN corepack enable

WORKDIR /app

# ----------------------------------------------------------------
# Dépendances système pour les addons natifs (onlyBuiltDependencies)
#
# authenticate-pam → node-gyp compile depuis les sources, PAM headers obligatoires
#   Pas de binaire précompilé npm → libpam0g-dev indispensable
#
# @napi-rs/canvas → binaire précompilé NAPI-RS pour linux-x64,
#   mais le postinstall vérifie les headers Cairo — on les installe
#   pour le cas où le fallback de compilation est déclenché
#
# @lydell/node-pty → node-gyp, build-essential + python3 nécessaires
#
# node-llama-cpp → binaire précompilé CPU disponible pour linux-x64,
#   cmake en fallback si le prebuilt ne correspond pas à l'environnement
# ----------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Outils de compilation node-gyp
    build-essential \
    python3 \
    # authenticate-pam (compile from source, no prebuilt)
    libpam0g-dev \
    # @napi-rs/canvas (Cairo renderer)
    pkg-config \
    libcairo2-dev \
    libpango1.0-dev \
    libjpeg-dev \
    libgif-dev \
    librsvg2-dev \
    # node-llama-cpp (compile fallback)
    cmake \
    && rm -rf /var/lib/apt/lists/*

# Packages APT supplémentaires via build arg (optionnel, hérité de l'upstream)
ARG BITTERBOT_DOCKER_APT_PACKAGES=""
RUN if [ -n "$BITTERBOT_DOCKER_APT_PACKAGES" ]; then \
      apt-get update && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $BITTERBOT_DOCKER_APT_PACKAGES && \
      apt-get clean && \
      rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*; \
    fi

# Copie des manifestes pour optimiser le cache de la couche pnpm install.
# Note : ui/package.json n'existe pas dans ce repo — COPY supprimé.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
COPY scripts ./scripts

# patches/ absent du repo (aucun patchedDependencies dans package.json)
RUN mkdir -p patches

# --no-frozen-lockfile : précaution pour les déphasages lockfile/package.json
# fréquents en upstream actif ; pnpm résout sans erreur.
RUN pnpm install --no-frozen-lockfile

COPY . .
RUN pnpm build
# Note : `pnpm ui:build` est dans le Dockerfile upstream mais le script
# n'existe pas dans package.json à ce commit — supprimé intentionnellement.

ENV NODE_ENV=production

# Autorise l'écriture de fichiers temporaires en runtime.
RUN chown -R node:node /app

# Sécurité : exécution en tant que non-root (uid 1000 = node)
USER node

CMD ["node", "bitterbot.mjs", "gateway", "--allow-unconfigured"]
