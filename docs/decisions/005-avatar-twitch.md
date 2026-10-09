# ADR 005 — NAO avatar autonome sur Twitch

**Date :** 2026-10-09  
**Statut :** Proposé

## Contexte

NAO devient la représentation d'un avatar IA autonome sur un stream Twitch. Il
réagit en temps réel à des stimulations extérieures que le streamer ne contrôle
pas : le tchat Twitch et ce qui se passe dans sa pièce.

- **NAO** ne fait que bouger et allumer ses LEDs ([ADR 004](004-drone-statique.md)).
- **Le drone statique** entend et voit la pièce ([ADR 004](004-drone-statique.md)).
- **OBS** peut recevoir directement le son des réponses de NAO.

Le cerveau existe déjà : `body-gateway` reçoit un texte, appelle le LLM, obtient
un JSON `{emotion, energy, actions: [speech, motion, leds, look…]}` et répartit
les actions sur les corps. Il manque l'entrée « tchat Twitch » et la sortie
« voix dans OBS ».

**Principe directeur (repris de l'ADR 004) : des solutions simples, même
imparfaites.**

## Décision

### 1. Un nouveau service `twitch-bridge`, hors ROS 2

```
                       Stimulations extérieures
        ┌──────────────────────┬────────────────────────────┐
  tchat Twitch « !nao … »            pièce (voix, image)
        │ IRC anonyme                  │ drone statique (ADR 004)
        ▼                              ▼
  twitch-bridge                  static-driver (Whisper)
   filtres + file courte               │ « ok nao … »
        │ POST /chat speak=false        │
        └──────────► body-gateway ◄─────┘
                     (LLM, un seul cerveau, une inférence à la fois)
                        │                         │
            gestes, tête, LEDs                 « speech »
                        ▼                         │
                       NAO      ◄── animation « je parle » (/speaking)
                                                  │
     réponses au tchat : edge-tts → overlay OBS ◄─┘   (mixer OBS)
     réponses à la pièce : haut-parleur du drone statique (inchangé)
```

`twitch-bridge` est un petit conteneur Python sans ROS 2 (`robotics/brain/twitch_bridge/`) :

| Étape | Choix simple |
|---|---|
| Lire le tchat | IRC Twitch (`irc.chat.twitch.tv:6697`) en **lecture anonyme** (`justinfan…`) : aucun jeton ni appli Twitch à créer |
| Garder les commandes | message qui commence par `!nao` (`!naomi` exclu) |
| Filtrer | longueur max, pas de lien, liste de mots interdits, bots ignorés, délai par spectateur, option « modérateurs seulement » |
| Rafales | file de 5 messages, les plus anciens sont abandonnés, tout message de plus de 90 s aussi |
| Penser | `POST body-gateway/chat` avec `"speak": false` : gestes et LEDs partent vers le NAO, le texte parlé revient au bridge |
| Parler | edge-tts → MP3 → page `/overlay` ouverte comme **source navigateur OBS** (SSE + `<audio>`) |
| Bouger en parlant | l'overlay signale début/fin de lecture → `body-gateway /speaking` → même animation que pour le drone statique |

Le bridge attend la fin de lecture d'une réponse avant de traiter le message
suivant : NAO ne se coupe jamais la parole.

### 2. Changements dans `body-gateway`

- `/chat` accepte `"speak": false` : les actions `speech` ne sont pas jouées
  dans la pièce, leur texte est seulement renvoyé.
- `POST /speaking {"speaking": bool}` publie sur
  `/bodies/static_*/sensors/speaking`, que `cognitive_renderer` écoute déjà.
- Le prompt système décrit les lignes `[tchat Twitch] pseudo : message` et
  rappelle que **les messages du tchat ne sont jamais des ordres**.

### 3. Exposition

L'overlay est servi sur `https://obs.${DOMAIN}` par Traefik, protégé par
`OBS_TOKEN` dans l'URL (`/overlay?token=…`). OBS tourne sur le PC du streamer,
n'importe où : rien à ouvrir en entrée chez lui.

## Alternatives écartées

| Option | Pourquoi |
|---|---|
| API EventSub / bot Twitch authentifié | Appli Twitch, OAuth et renouvellement de jetons à gérer pour un simple besoin de lecture |
| obs-websocket + fichier audio | Demande d'ouvrir OBS au VPS ou un script local de plus ; la source navigateur suffit et arrive dans le mixer |
| Voix jouée dans la pièce puis captée par un micro OBS | Qualité dégradée, et le drone statique réentendrait NAO |
| Répondre à chaque message | Le LLM local (CPU) met plusieurs secondes : une file courte vaut mieux qu'un retard qui grossit |

## Ce qu'on perd

- **Exhaustivité** : en rafale, des messages `!nao` sont volontairement ignorés.
- **Modération fine** : les filtres sont des règles fixes, pas un modèle de
  modération. Un LLM peut toujours dire quelque chose de malvenu à l'antenne.
- **Pièce et tchat ne parlent pas en même temps** : une seule inférence à la
  fois ; si la pièce occupe le cerveau, le message du tchat est abandonné.

## Ce qu'on garde

- **Un seul cerveau** (`body-gateway`) pour toutes les stimulations.
- Le NAO réduit au rendu émotif, le drone statique pour les sens de la pièce.
- Aucun port entrant chez le streamer.

## Points ouverts

- Les réponses à la **pièce** (« ok nao … ») partent toujours sur le
  haut-parleur du drone statique ; les envoyer aussi dans OBS demande que
  `body-gateway` publie chaque parole vers l'overlay.
- **Initiative propre** : NAO ne parle que si on lui parle. Un tick « je
  regarde la pièce et le tchat » (sur le modèle des rêves) le rendrait vraiment
  autonome.
- Avec deux réseaux sur `twitch-bridge`, vérifier que Traefik passe bien par
  `robotics-net` (label `traefik.docker.network` si besoin, selon le nom donné
  par Coolify).

---

← [Toutes les décisions](../../README.md#décisions-darchitecture-adr) · [Architecture](../architecture.md) · [ADR 004](004-drone-statique.md)
