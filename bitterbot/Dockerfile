# Dockerfile maintenu par monolithoid/ia-agent
# Remplace le Dockerfile upstream de Bitterbot-AI/bitterbot-desktop
# qui référence des chemins absents du repo (ui/, patches/, pnpm ui:build).
#
# Différences avec l'upstream :
#   - COPY ui/package.json supprimé  (ui/ n'existe pas dans le repo)
#   - COPY patches supprimé + mkdir patches (pas de patchedDependencies dans package.json)
#   - pnpm ui:build supprimé        (script absent du package.json)
#
# À mettre à jour si le repo upstream restructure ces chemins.

FROM node:22-bookworm

# Install Bun (required for build scripts)
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"

RUN corepack enable

WORKDIR /app

# Packages APT optionnels (addons natifs, etc.)
ARG BITTERBOT_DOCKER_APT_PACKAGES=""
RUN if [ -n "$BITTERBOT_DOCKER_APT_PACKAGES" ]; then \
      apt-get update && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $BITTERBOT_DOCKER_APT_PACKAGES && \
      apt-get clean && \
      rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*; \
    fi

# Copie des manifestes pour optimiser le cache de la couche pnpm install.
# Note : ui/package.json et patches/ n'existent pas dans ce repo — ignorés.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc ./
COPY scripts ./scripts

# patches/ absent du repo (aucun patchedDependencies dans package.json)
# On le crée vide pour ne pas casser d'éventuels scripts qui le référencent.
RUN mkdir -p patches

RUN pnpm install --frozen-lockfile

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
